package sandbox

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Pinned on both sides: tests/test_sandbox.py has the same constant.
const keyrepoKey = "f349a213ae58eca6ead49dabb28c2b14"

func TestEnvprepCopyMatchesThePythonOriginal(t *testing.T) {
	original, err := os.ReadFile("../../../../habodek/brain/sandbox/envprep.py")
	if os.IsNotExist(err) {
		t.Skip("not in the full repository")
	}
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(original, envprepScript) {
		t.Fatal("copy habodek/brain/sandbox/envprep.py to agent/internal/pkg/sandbox/envprep.py")
	}
	m := regexp.MustCompile(`(?m)^SCRIPT_VERSION = "([^"]+)"`).FindSubmatch(envprepScript)
	if m == nil || string(m[1]) != ScriptVersion {
		t.Fatalf("SCRIPT_VERSION %q, Go ScriptVersion %q", m, ScriptVersion)
	}
}

func TestCacheKeyMatchesThePythonImplementation(t *testing.T) {
	recipe, err := os.ReadFile("testdata/keyrepo/.habodek.md")
	if err != nil {
		t.Fatal(err)
	}
	got, err := CacheKey("testdata/keyrepo", string(recipe), "docker:python:3.12")
	if err != nil {
		t.Fatal(err)
	}
	if got != keyrepoKey {
		t.Fatalf("key %s, want %s (the Python side pins the same value)", got, keyrepoKey)
	}
}

func TestDockerRunIsLockedDown(t *testing.T) {
	d := &Docker{Image: "habodek-runner:1", User: "10001:10001",
		MapPath: func(p string) string { return "/host" + p }}
	args := strings.Join(d.ExecArgs("habodek-run-x", ExecSpec{Work: "/w/run/work", Venv: "/c/venvs/k",
		Cmd: "python -m pytest -q", MemoryMB: 1024, CPUs: 1.5}), " ")
	for _, want := range []string{"--network none", "--read-only", "--cap-drop ALL",
		"--security-opt no-new-privileges", "--user 10001:10001", "--memory 1024m", "--memory-swap 1024m",
		"--cpus 1.5", "--pids-limit 512", "-v /host/w/run/work:/work", "-v /host/c/venvs/k:/venv:ro",
		"habodek-runner:1 sh -c python -m pytest -q"} {
		if !strings.Contains(args, want) {
			t.Errorf("exec args lack %q:\n%s", want, args)
		}
	}
	for _, banned := range []string{"--privileged", "docker.sock", "--network host", "--network bridge"} {
		if strings.Contains(args, banned) {
			t.Errorf("exec args contain %q", banned)
		}
	}
	prep := strings.Join(d.PrepArgs("habodek-prep-x", PrepSpec{Src: "/w/head", Venv: "/c/venvs/k",
		Script: "/c/envprep-1.py", Recipe: "/w/base/.habodek.md", IndexURL: "https://artifactory/simple"}), " ")
	for _, want := range []string{"--network bridge", "--user 10001:10001", "--cap-drop ALL",
		"-v /host/w/head:/src:ro", "-v /host/c/venvs/k:/venv",
		"python /opt/habodek/envprep.py --src /src --venv /venv --recipe /opt/habodek/recipe.md",
		"--index-url https://artifactory/simple"} {
		if !strings.Contains(prep, want) {
			t.Errorf("prep args lack %q:\n%s", want, prep)
		}
	}
}

// fakeBackend records what it was asked; env-prep writes the marker.
type fakeBackend struct {
	preps atomic.Int32
	execs atomic.Int32
	last  ExecSpec
}

func (f *fakeBackend) Name() string { return "fake" }
func (f *fakeBackend) Tag() string  { return "fake" }
func (f *fakeBackend) Prep(_ context.Context, spec PrepSpec) ([]byte, error) {
	f.preps.Add(1)
	return []byte("ok"), os.WriteFile(filepath.Join(spec.Venv, Marker), []byte("{}"), 0o644)
}
func (f *fakeBackend) Exec(_ context.Context, spec ExecSpec) (ExecResult, error) {
	f.execs.Add(1)
	f.last = spec
	data, _ := os.ReadFile(filepath.Join(spec.Work, "probe.txt"))
	return ExecResult{ExitCode: 3, Output: append([]byte("ran: "), data...)}, nil
}

func newService(t *testing.T, b Backend) (*Service, string) {
	t.Helper()
	dir := t.TempDir()
	svc, err := New(Config{CacheDir: filepath.Join(dir, "cache"), WorkDir: filepath.Join(dir, "runs")}, b)
	if err != nil {
		t.Fatal(err)
	}
	head := filepath.Join(dir, "head")
	_ = os.MkdirAll(filepath.Join(head, ".git"), 0o755)
	_ = os.WriteFile(filepath.Join(head, "requirements.txt"), []byte("x==1\n"), 0o644)
	_ = os.WriteFile(filepath.Join(head, ".git", "config"), []byte("secret"), 0o644)
	return svc, head
}

func post(t *testing.T, srv *httptest.Server, token string, req any) (*http.Response, Response) {
	t.Helper()
	body, _ := json.Marshal(req)
	r, _ := http.NewRequest(http.MethodPost, srv.URL+Path, bytes.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out Response
	if resp.StatusCode == http.StatusOK {
		_ = json.NewDecoder(resp.Body).Decode(&out)
	}
	return resp, out
}

func TestServiceAuthRootsAndFiles(t *testing.T) {
	fb := &fakeBackend{}
	svc, head := newService(t, fb)
	trace := t.TempDir()
	svc.Register("tok", Job{ID: "j1", Roots: map[string]string{"head": head}, TraceDir: trace})
	srv := httptest.NewServer(svc)
	defer srv.Close()

	if resp, _ := post(t, srv, "wrong", Request{Cmd: "true"}); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("unknown token: %d", resp.StatusCode)
	}
	for _, bad := range []Request{
		{Cmd: "true", Root: "/etc"},
		{Cmd: "true", Root: "dep:x@1"},
		{Cmd: ""},
		{Cmd: "true", Files: map[string]string{"../escape.py": "x"}},
		{Cmd: "true", Files: map[string]string{"/etc/passwd": "x"}},
		{Cmd: "true", Files: map[string]string{"a/../../b": "x"}},
	} {
		if resp, _ := post(t, srv, "tok", bad); resp.StatusCode != http.StatusBadRequest {
			t.Errorf("%+v: status %d", bad, resp.StatusCode)
		}
	}
	if fb.execs.Load() != 0 {
		t.Fatal("a rejected request ran")
	}

	resp, out := post(t, srv, "tok", Request{Cmd: "cat probe.txt", Files: map[string]string{"probe.txt": "hello"},
		TimeoutS: 99999})
	if resp.StatusCode != http.StatusOK || out.ExitCode != 3 || out.Output != "ran: hello" || !out.Env.OK || out.Env.Cached {
		t.Fatalf("run: %d %+v", resp.StatusCode, out)
	}
	if fb.last.Timeout != maxTimeout {
		t.Fatalf("timeout %s not clamped", fb.last.Timeout)
	}
	_, out = post(t, srv, "tok", Request{Cmd: "true"})
	if !out.Env.Cached || fb.preps.Load() != 1 {
		t.Fatalf("venv not reused: preps=%d %+v", fb.preps.Load(), out.Env)
	}
	if _, err := os.Stat(filepath.Join(head, "probe.txt")); !os.IsNotExist(err) {
		t.Fatal("a run wrote into the worktree")
	}
	log, _ := os.ReadFile(filepath.Join(trace, "sandbox.jsonl"))
	if strings.Count(string(log), "\n") != 2 || !strings.Contains(string(log), `"exit_code":3`) {
		t.Fatalf("sandbox.jsonl = %s", log)
	}
	svc.Unregister("tok")
	if resp, _ := post(t, srv, "tok", Request{Cmd: "true"}); resp.StatusCode != http.StatusForbidden {
		t.Fatal("token still valid after unregister")
	}
}

func TestCopySkipsGitAndCaches(t *testing.T) {
	src := t.TempDir()
	for _, p := range []string{".git/config", "__pycache__/x.pyc", "pkg/a.py", "node_modules/m.js"} {
		_ = os.MkdirAll(filepath.Join(src, filepath.Dir(p)), 0o755)
		_ = os.WriteFile(filepath.Join(src, p), []byte("x"), 0o644)
	}
	_ = os.Symlink("/etc/passwd", filepath.Join(src, "pkg", "link"))
	dst := filepath.Join(t.TempDir(), "w")
	if err := copyTree(src, dst); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dst, "pkg/a.py")); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{".git", "__pycache__", "node_modules"} {
		if _, err := os.Stat(filepath.Join(dst, p)); !os.IsNotExist(err) {
			t.Errorf("%s copied", p)
		}
	}
	if target, _ := os.Readlink(filepath.Join(dst, "pkg/link")); target != "/etc/passwd" {
		t.Error("symlink not kept as a link")
	}
}

func TestPrepFailureIsReportedAndNotCached(t *testing.T) {
	b := &failingPrep{}
	svc, head := newService(t, b)
	job := Job{ID: "j", Roots: map[string]string{"head": head}}
	out := svc.Run(context.Background(), job, Request{Cmd: "true"})
	if out.ExitCode != -1 || out.Env.OK || !strings.Contains(out.Error, "env-prep failed") ||
		!strings.Contains(strings.Join(out.Env.Log, "\n"), "No matching distribution") {
		t.Fatalf("%+v", out)
	}
	svc.Run(context.Background(), job, Request{Cmd: "true"})
	if b.n != 2 {
		t.Fatalf("a failed venv was cached (preps=%d)", b.n)
	}
}

type failingPrep struct{ n int }

func (f *failingPrep) Name() string { return "failing" }
func (f *failingPrep) Tag() string  { return "failing" }
func (f *failingPrep) Prep(context.Context, PrepSpec) ([]byte, error) {
	f.n++
	return []byte("ERROR: No matching distribution found for x==1"), fmt.Errorf("env-prep exited 1")
}
func (f *failingPrep) Exec(context.Context, ExecSpec) (ExecResult, error) {
	return ExecResult{}, fmt.Errorf("must not run")
}

// TestLocalRunCannotOpenANetworkConnection is half of M9's done-when: a sandboxed
// command cannot reach even a server on the host's own loopback.
func TestLocalRunCannotOpenANetworkConnection(t *testing.T) {
	if !LocalAvailable() {
		t.Skip("no unprivileged user+network namespaces here")
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	var accepted atomic.Int32
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			accepted.Add(1)
			c.Close()
		}
	}()
	port := ln.Addr().(*net.TCPAddr).Port

	svc, head := newService(t, &Local{})
	_ = os.Remove(filepath.Join(head, "requirements.txt")) // nothing to install
	job := Job{ID: "net", Roots: map[string]string{"head": head}}
	probe := fmt.Sprintf(`import socket
for host, port in (("127.0.0.1", %d), ("10.0.0.1", 80), ("1.1.1.1", 53)):
    try:
        socket.create_connection((host, port), timeout=3)
        print("CONNECTED", host)
    except OSError as exc:
        print("blocked", host, exc.errno)
`, port)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	out := svc.Run(ctx, job, Request{Cmd: "python probe.py", Files: map[string]string{"probe.py": probe}})
	if out.ExitCode != 0 || out.Error != "" {
		t.Fatalf("probe did not run: %+v", out)
	}
	if strings.Contains(out.Output, "CONNECTED") || strings.Count(out.Output, "blocked") != 3 || accepted.Load() != 0 {
		t.Fatalf("network reachable from the sandbox:\n%s", out.Output)
	}
	// The host can reach the listener: the block is the sandbox, not the test.
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	c.Close()

	timed := svc.Run(ctx, job, Request{Cmd: "sleep 60", TimeoutS: 5})
	if !timed.TimedOut || timed.Seconds > 20 {
		t.Fatalf("timeout not enforced: %+v", timed)
	}
}

func TestPrefixMap(t *testing.T) {
	m := PrefixMap(map[string]string{"/data": "/srv/hbk", "/data/cache": "/fast/cache"})
	for in, want := range map[string]string{"/data/jobs/1": "/srv/hbk/jobs/1", "/data": "/srv/hbk",
		"/data/cache/venvs/k": "/fast/cache/venvs/k", "/database/x": "/database/x", "/other": "/other"} {
		if got := m(in); got != want {
			t.Errorf("%s -> %s, want %s", in, got, want)
		}
	}
	if PrefixMap(nil) != nil {
		t.Error("an empty map is no mapping")
	}
}

func TestDockerDefaultsToTheControlPlanesOwnUser(t *testing.T) {
	want := fmt.Sprintf("--user %d:%d", os.Getuid(), os.Getgid())
	d := &Docker{Image: "img"}
	for _, args := range [][]string{d.ExecArgs("n", ExecSpec{Work: "/w", Venv: "/v", Cmd: "true"}),
		d.PrepArgs("n", PrepSpec{Src: "/s", Venv: "/v", Script: "/e.py"})} {
		if !strings.Contains(strings.Join(args, " "), want) {
			t.Fatalf("%v lacks %s", args, want)
		}
	}
}
