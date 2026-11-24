package brain

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/sandbox"
)

// Tokens is the part of the LLM proxy the runner needs.
type Tokens interface {
	Register(job llmproxy.Job) string
	Unregister(token string)
}

// Sandboxes is the part of the sandbox service the runner needs: the job's token may
// run the job's roots for as long as the job runs.
type Sandboxes interface {
	Register(token string, job sandbox.Job)
	Unregister(token string)
}

// RunnerConfig configures how the brain subprocess is started.
type RunnerConfig struct {
	Command          string   // e.g. "habodek-brain" or "/opt/habodek/bin/habodek-brain"
	Args             []string // prepended to the subcommand (e.g. ["-m", "habodek.brain.cli"])
	WorkDir          string
	TraceDir         string
	ProxyURL         string
	Timeout          time.Duration
	InternalPackages string
	PipIndexURL      string
	SandboxURL       string   // how the brain reaches the sandbox service ("" = no sandbox)
	ExtraEnv         []string // KEY=VALUE, for tests
}

// Runner runs one brain subprocess per job.
type Runner struct {
	cfg       RunnerConfig
	tokens    Tokens
	sandboxes Sandboxes
}

func NewRunner(cfg RunnerConfig, tokens Tokens) *Runner {
	if cfg.Timeout <= 0 {
		cfg.Timeout = 90 * time.Minute
	}
	return &Runner{cfg: cfg, tokens: tokens}
}

// WithSandbox gives the brain the `run` tool: its job token may run commands in the
// job's head, base and other repositories through the sandbox service.
func (r *Runner) WithSandbox(s Sandboxes) *Runner {
	r.sandboxes = s
	return r
}

// SandboxRoots are the roots of a job the brain may run: never dependency sources or
// site-packages, which are read-only references.
func SandboxRoots(ws Workspace) map[string]string {
	roots := map[string]string{"head": ws.Head}
	if ws.Base != "" {
		roots["base"] = ws.Base
	}
	for name, dir := range ws.OtherRepos {
		roots["repo:"+name] = dir
	}
	return roots
}

// TraceDir is where a job's trace lives (kept after the job; the trace viewer reads it).
func (r *Runner) TraceDir(jobID string) string { return filepath.Join(r.cfg.TraceDir, jobID) }

// JobDir is the job's scratch directory (worktrees, job.json); disposable.
func (r *Runner) JobDir(jobID string) string { return filepath.Join(r.cfg.WorkDir, jobID) }

// Run writes job.json, runs `habodek-brain <kind>`, and reads result.json.
// The subprocess gets a cleared environment: no GitLab token, no gateway key — only
// the proxy URL and a token valid for this job.
func (r *Runner) Run(ctx context.Context, job Job) (*Result, error) {
	job.Contract = ContractVersion
	if job.Kind != "review" && job.Kind != "thread" {
		return nil, fmt.Errorf("unknown job kind %q", job.Kind)
	}
	jobDir := r.JobDir(job.JobID)
	traceDir := r.TraceDir(job.JobID)
	for _, dir := range []string{jobDir, traceDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	jobPath := filepath.Join(jobDir, "job.json")
	resultPath := filepath.Join(traceDir, "result.json")
	data, err := json.MarshalIndent(Envelope{Job: job}, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(jobPath, data, 0o644); err != nil {
		return nil, err
	}
	_ = os.Remove(resultPath)

	token := r.tokens.Register(llmproxy.Job{ID: job.JobID,
		Priority: llmproxy.PriorityFromName(job.Priority), TraceDir: traceDir})
	defer r.tokens.Unregister(token)
	sandboxOn := r.sandboxes != nil && r.cfg.SandboxURL != ""
	if sandboxOn {
		r.sandboxes.Register(token, sandbox.Job{ID: job.JobID, Roots: SandboxRoots(job.Workspace),
			TraceDir: traceDir})
		defer r.sandboxes.Unregister(token)
	}

	ctx, cancel := context.WithTimeout(ctx, r.cfg.Timeout)
	defer cancel()
	args := append(append([]string{}, r.cfg.Args...), job.Kind, "--job", jobPath,
		"--out", resultPath, "--trace", traceDir)
	cmd := exec.CommandContext(ctx, r.cfg.Command, args...)
	cmd.Dir = jobDir
	cmd.Env = r.env(token)
	if sandboxOn {
		cmd.Env = append(cmd.Env, "HABODEK_SANDBOX_URL="+r.cfg.SandboxURL)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	runErr := cmd.Run()
	_ = os.WriteFile(filepath.Join(traceDir, "brain.stderr.log"), stderr.Bytes(), 0o644)

	result, readErr := readResult(resultPath)
	if readErr != nil {
		if runErr != nil {
			return nil, fmt.Errorf("brain %s failed: %v: %s", job.Kind, runErr, tail(stderr.String(), 800))
		}
		return nil, fmt.Errorf("brain %s wrote no result: %v", job.Kind, readErr)
	}
	if result.Contract != ContractVersion {
		return nil, fmt.Errorf("brain contract %d, control plane expects %d", result.Contract, ContractVersion)
	}
	if result.Status == "failed" {
		return result, fmt.Errorf("brain %s failed: %s", job.Kind, result.Reason)
	}
	return result, nil
}

func (r *Runner) env(token string) []string {
	env := []string{
		"PATH=" + os.Getenv("PATH"),
		// The real HOME: user-site packages and pip's cache live under it. It is not a
		// secret; GitLab and gateway credentials are what stays out of this list.
		"HOME=" + os.Getenv("HOME"),
		"LANG=C.UTF-8",
		"HABODEK_LLM_BASE_URL=" + r.cfg.ProxyURL,
		"HABODEK_LLM_TOKEN=" + token,
		// The proxy is the real fence; the brain's own limiter must not be tighter.
		"HABODEK_RPM=600",
		"HABODEK_DEPS_CACHE=" + filepath.Join(r.cfg.WorkDir, "deps-cache"),
	}
	if r.cfg.InternalPackages != "" {
		env = append(env, "HABODEK_INTERNAL_PACKAGES="+r.cfg.InternalPackages)
	}
	if r.cfg.PipIndexURL != "" {
		env = append(env, "HABODEK_PIP_INDEX_URL="+r.cfg.PipIndexURL)
	}
	for _, key := range []string{"HABODEK_MODELS_YAML", "HABODEK_MODEL_QWEN", "HABODEK_MODEL_GLM",
		"HABODEK_PIP_FIND_LINKS", "VIRTUAL_ENV", "PYTHONPATH"} {
		if v, ok := os.LookupEnv(key); ok {
			env = append(env, key+"="+v)
		}
	}
	return append(env, r.cfg.ExtraEnv...)
}

func readResult(path string) (*Result, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var result Result
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("result.json: %w", err)
	}
	return &result, nil
}

func tail(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}
