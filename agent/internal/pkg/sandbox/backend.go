package sandbox

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// PrepSpec asks a backend to build a venv for one source tree (env-prep).
type PrepSpec struct {
	Src       string // the worktree whose dependency files are read
	Venv      string // empty directory the venv is created in
	Recipe    string // .habodek.md to read install/tools lines from ("" = none)
	Script    string // envprep.py on the host
	IndexURL  string // Artifactory; "" with FindLinks = offline
	FindLinks []string
	Timeout   time.Duration
}

// ExecSpec asks a backend to run one command in a prepared copy.
type ExecSpec struct {
	Work     string // throwaway copy of the root (writable)
	Venv     string // the cached venv (mounted read-only where the backend can)
	Cmd      string // run with sh -c
	Timeout  time.Duration
	MemoryMB int
	CPUs     float64
	Pids     int
}

// ExecResult is what a command did.
type ExecResult struct {
	ExitCode int
	TimedOut bool
	Output   []byte // combined stdout and stderr
	Seconds  float64
}

// Backend runs env-prep and commands. Docker in production; Local on a dev box.
type Backend interface {
	Name() string
	// Tag names the interpreter env-prep builds with; part of the venv cache key.
	Tag() string
	Prep(ctx context.Context, spec PrepSpec) ([]byte, error)
	Exec(ctx context.Context, spec ExecSpec) (ExecResult, error)
}

func prepArgs(script, src, venv, recipe, indexURL string, findLinks []string) []string {
	args := []string{script, "--src", src, "--venv", venv}
	if recipe != "" {
		args = append(args, "--recipe", recipe)
	}
	if indexURL != "" {
		args = append(args, "--index-url", indexURL)
	}
	for _, f := range findLinks {
		args = append(args, "--find-links", f)
	}
	return args
}

// runCmd runs cmd with a timeout, killing its whole process group on expiry.
func runCmd(ctx context.Context, cmd *exec.Cmd, timeout time.Duration, onTimeout func()) (ExecResult, error) {
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	started := time.Now()
	if err := cmd.Start(); err != nil {
		return ExecResult{}, err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	var waitErr error
	timedOut := false
	select {
	case waitErr = <-done:
	case <-timer.C:
		timedOut = true
	case <-ctx.Done():
		timedOut = true
	}
	if timedOut {
		if onTimeout != nil {
			onTimeout()
		}
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		waitErr = <-done
	}
	res := ExecResult{TimedOut: timedOut, Output: out.Bytes(),
		Seconds: float64(time.Since(started).Milliseconds()) / 1000}
	var exitErr *exec.ExitError
	switch {
	case timedOut:
		res.ExitCode = -9
	case waitErr == nil:
		res.ExitCode = 0
	case errors.As(waitErr, &exitErr):
		res.ExitCode = exitErr.ExitCode()
	default:
		return res, waitErr
	}
	return res, nil
}

// ---------------------------------------------------------------- local

// Local runs env-prep with the host's python, and commands in a fresh user, network
// and PID namespace (`unshare`): no interface but a downed loopback, so not even the
// host's 127.0.0.1 is reachable. The filesystem is the host's: use it for development
// and tests; production uses Docker.
type Local struct {
	Python string // default python3
}

func (l *Local) python() string {
	if l.Python != "" {
		return l.Python
	}
	return "python3"
}

func (l *Local) Name() string { return "local" }

func (l *Local) Tag() string {
	out, err := exec.Command(l.python(), "-c",
		"import sys; print('%d.%d' % sys.version_info[:2])").Output()
	if err != nil {
		return "local:" + l.python()
	}
	return "local:" + l.python() + ":" + strings.TrimSpace(string(out))
}

// LocalAvailable reports whether unprivileged user+network namespaces work here.
func LocalAvailable() bool {
	return exec.Command("unshare", "--user", "--map-root-user", "--net", "true").Run() == nil
}

func (l *Local) Prep(ctx context.Context, spec PrepSpec) ([]byte, error) {
	cmd := exec.Command(l.python(), prepArgs(spec.Script, spec.Src, spec.Venv, spec.Recipe,
		spec.IndexURL, spec.FindLinks)...)
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + os.Getenv("HOME"), "LANG=C.UTF-8",
		"PIP_DISABLE_PIP_VERSION_CHECK=1"}
	res, err := runCmd(ctx, cmd, spec.Timeout, nil)
	if err != nil {
		return res.Output, err
	}
	if res.TimedOut {
		return res.Output, fmt.Errorf("env-prep timed out after %s", spec.Timeout)
	}
	if res.ExitCode != 0 {
		return res.Output, fmt.Errorf("env-prep exited %d", res.ExitCode)
	}
	return res.Output, nil
}

func (l *Local) Exec(ctx context.Context, spec ExecSpec) (ExecResult, error) {
	mem := spec.MemoryMB
	if mem <= 0 {
		mem = 4096
	}
	args := []string{"--user", "--map-root-user", "--net", "--pid", "--fork", "--kill-child",
		"prlimit", "--as=" + strconv.Itoa(mem*1024*1024), "--nofile=4096", "--",
		"sh", "-c", spec.Cmd}
	cmd := exec.Command("unshare", args...)
	cmd.Dir = spec.Work
	home := spec.Work + ".home"
	_ = os.MkdirAll(home, 0o755)
	cmd.Env = []string{
		"PATH=" + spec.Venv + "/bin:/usr/local/bin:/usr/bin:/bin",
		"HOME=" + home, "TMPDIR=" + home, "LANG=C.UTF-8", "CI=1",
		"PYTHONPATH=" + spec.Work + ":" + spec.Work + "/src",
		"PYTHONDONTWRITEBYTECODE=1", "VIRTUAL_ENV=" + spec.Venv,
	}
	return runCmd(ctx, cmd, spec.Timeout, nil)
}

// ---------------------------------------------------------------- docker

// Docker runs both steps as sibling containers through the docker CLI (the control
// plane owns the socket; the brain never does).
//
//   - env-prep: on PrepNetwork (the VPN's internal network, to reach Artifactory), the
//     worktree read-only, the venv directory mounted at /venv.
//   - run: --network none, read-only root filesystem, a tmpfs /tmp, all capabilities
//     dropped, no-new-privileges, an unprivileged user, CPU/memory/PID limits, the copy
//     at /work and the venv read-only at /venv (the same path it was built at).
type Docker struct {
	Binary      string // default "docker"
	Image       string // runner image: python 3.12 + pytest
	PrepNetwork string // default "bridge"
	// User both containers run as (default: this process's uid:gid). Running as the
	// control plane's own unprivileged user means everything a run or env-prep
	// writes is the control plane's to delete; capabilities are dropped either way.
	User string
	// MapPath translates a path on the control plane's filesystem to the Docker
	// host's, when the control plane itself runs in a container (M12). nil = same.
	MapPath func(string) string
}

func (d *Docker) bin() string {
	if d.Binary != "" {
		return d.Binary
	}
	return "docker"
}

func (d *Docker) user() string {
	if d.User != "" {
		return d.User
	}
	return fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid())
}

func (d *Docker) host(p string) string {
	if d.MapPath != nil {
		return d.MapPath(p)
	}
	return p
}

func (d *Docker) Name() string { return "docker" }
func (d *Docker) Tag() string  { return "docker:" + d.Image }

func containerName(prefix string) string {
	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	return prefix + hex.EncodeToString(buf)
}

// PrepArgs is the `docker run` argument list for env-prep (exported for tests).
func (d *Docker) PrepArgs(name string, spec PrepSpec) []string {
	network := d.PrepNetwork
	if network == "" {
		network = "bridge"
	}
	args := []string{"run", "--rm", "--name", name, "--network", network,
		"--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", d.user(),
		"-e", "HOME=/tmp", "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
		"-v", d.host(spec.Src) + ":/src:ro",
		"-v", d.host(spec.Venv) + ":/venv",
		"-v", d.host(spec.Script) + ":/opt/habodek/envprep.py:ro"}
	recipe := ""
	if spec.Recipe != "" {
		args = append(args, "-v", d.host(spec.Recipe)+":/opt/habodek/recipe.md:ro")
		recipe = "/opt/habodek/recipe.md"
	}
	links := make([]string, 0, len(spec.FindLinks))
	for i, f := range spec.FindLinks {
		mount := fmt.Sprintf("/opt/habodek/links%d", i)
		args = append(args, "-v", d.host(f)+":"+mount+":ro")
		links = append(links, mount)
	}
	args = append(args, d.Image, "python")
	return append(args, prepArgs("/opt/habodek/envprep.py", "/src", "/venv", recipe,
		spec.IndexURL, links)...)
}

// ExecArgs is the `docker run` argument list for a command (exported for tests).
func (d *Docker) ExecArgs(name string, spec ExecSpec) []string {
	user := d.user()
	mem := spec.MemoryMB
	if mem <= 0 {
		mem = 4096
	}
	cpus := spec.CPUs
	if cpus <= 0 {
		cpus = 2
	}
	pids := spec.Pids
	if pids <= 0 {
		pids = 512
	}
	return []string{"run", "--rm", "--name", name,
		"--network", "none", "--read-only",
		"--tmpfs", "/tmp:rw,exec,size=1g",
		"--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--user", user,
		"--memory", fmt.Sprintf("%dm", mem), "--memory-swap", fmt.Sprintf("%dm", mem),
		"--cpus", strconv.FormatFloat(cpus, 'f', -1, 64),
		"--pids-limit", strconv.Itoa(pids),
		"-v", d.host(spec.Work) + ":/work",
		"-v", d.host(spec.Venv) + ":/venv:ro",
		"-w", "/work",
		"-e", "HOME=/tmp", "-e", "TMPDIR=/tmp", "-e", "LANG=C.UTF-8", "-e", "CI=1",
		"-e", "PATH=/venv/bin:/usr/local/bin:/usr/bin:/bin",
		"-e", "PYTHONPATH=/work:/work/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
		"-e", "VIRTUAL_ENV=/venv",
		d.Image, "sh", "-c", spec.Cmd}
}

func (d *Docker) kill(name string) func() {
	return func() { _ = exec.Command(d.bin(), "rm", "-f", name).Run() }
}

func (d *Docker) Prep(ctx context.Context, spec PrepSpec) ([]byte, error) {
	name := containerName("habodek-prep-")
	cmd := exec.Command(d.bin(), d.PrepArgs(name, spec)...)
	res, err := runCmd(ctx, cmd, spec.Timeout, d.kill(name))
	if err != nil {
		return res.Output, err
	}
	if res.TimedOut {
		return res.Output, fmt.Errorf("env-prep timed out after %s", spec.Timeout)
	}
	if res.ExitCode != 0 {
		return res.Output, fmt.Errorf("env-prep exited %d", res.ExitCode)
	}
	return res.Output, nil
}

func (d *Docker) Exec(ctx context.Context, spec ExecSpec) (ExecResult, error) {
	name := containerName("habodek-run-")
	cmd := exec.Command(d.bin(), d.ExecArgs(name, spec)...)
	res, err := runCmd(ctx, cmd, spec.Timeout, d.kill(name))
	if err == nil && !res.TimedOut && res.ExitCode == 125 {
		// docker's own failure (image missing, bad flag), not the command's
		return res, fmt.Errorf("docker run failed: %s", strings.TrimSpace(tailBytes(res.Output, 400)))
	}
	return res, err
}

func tailBytes(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[len(b)-n:])
}

// PrefixMap returns a MapPath that rewrites the longest matching directory prefix
// (from -> to); paths outside every prefix are returned unchanged.
func PrefixMap(m map[string]string) func(string) string {
	if len(m) == 0 {
		return nil
	}
	return func(p string) string {
		best := ""
		for from := range m {
			if (p == from || strings.HasPrefix(p, from+"/")) && len(from) > len(best) {
				best = from
			}
		}
		if best == "" {
			return p
		}
		return m[best] + strings.TrimPrefix(p, best)
	}
}
