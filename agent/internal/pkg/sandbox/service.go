// Package sandbox runs repository code for the brain (handoff §3.6, M9).
//
// The brain asks `POST /internal/sandbox/v1/run` with its per-job token to run a
// command in a throwaway copy of one of its job's roots (head, base, repo:<name>). The
// service builds the root's venv first if it is not cached (env-prep, keyed by the
// dependency files and the recipe), then runs the command with no network.
//
// The brain names roots by label; it never passes a host path. Only roots registered
// for its token can be run.
package sandbox

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Path is where the run endpoint is mounted on the control plane's server.
const Path = "/internal/sandbox/v1/run"

// RecipeFile is the per-repository recipe (handoff M9), read from the target branch.
const RecipeFile = ".habodek.md"

// Marker is written into a venv by envprep.py when it is complete.
const Marker = "habodek-env.json"

//go:embed envprep.py
var envprepScript []byte

// EnvprepScript is the embedded env-prep script (a copy of habodek/brain/sandbox/envprep.py).
func EnvprepScript() []byte { return envprepScript }

const (
	defaultTimeout = 120 * time.Second
	maxTimeout     = 600 * time.Second
	maxOutput      = 200_000
	maxBody        = 4 << 20
)

var skipCopy = map[string]bool{".git": true, ".hg": true, ".venv": true, "venv": true,
	"node_modules": true, "__pycache__": true, ".mypy_cache": true, ".pytest_cache": true,
	".tox": true, ".ruff_cache": true}

// Job is what a token may run: its roots by label, and where its trace goes.
type Job struct {
	ID       string
	Roots    map[string]string // label -> directory (head, base, repo:<name>)
	TraceDir string
}

// Config of the service.
type Config struct {
	CacheDir    string // venvs/<key>/ and the env-prep script
	WorkDir     string // throwaway copies
	IndexURL    string // Artifactory simple index for env-prep
	FindLinks   []string
	PrepTimeout time.Duration
	MemoryMB    int
	CPUs        float64
	MaxParallel int // commands at once (env-prep included)
}

// Request is the brain's run request (same JSON as habodek/brain/sandbox/core.py).
type Request struct {
	Root     string            `json:"root"`
	Cmd      string            `json:"cmd"`
	Files    map[string]string `json:"files"`
	TimeoutS int               `json:"timeout_s"`
}

// Env describes the venv a run used.
type Env struct {
	Key    string   `json:"key"`
	OK     bool     `json:"ok"`
	Cached bool     `json:"cached"`
	Log    []string `json:"log,omitempty"`
}

// Response is what the run did.
type Response struct {
	ExitCode  int     `json:"exit_code"`
	TimedOut  bool    `json:"timed_out"`
	Seconds   float64 `json:"seconds"`
	Output    string  `json:"output"`
	Truncated bool    `json:"truncated"`
	Env       Env     `json:"env"`
	Error     string  `json:"error"`
}

// Service implements http.Handler for Path.
type Service struct {
	cfg     Config
	backend Backend
	jobs    sync.Map // token -> Job
	keyMu   sync.Mutex
	keys    map[string]*sync.Mutex
	sem     chan struct{}
	script  string
	logMu   sync.Mutex
}

// New creates the service and writes the env-prep script into the cache directory.
func New(cfg Config, backend Backend) (*Service, error) {
	if cfg.PrepTimeout <= 0 {
		cfg.PrepTimeout = 20 * time.Minute
	}
	if cfg.MaxParallel <= 0 {
		cfg.MaxParallel = 2
	}
	for _, dir := range []string{filepath.Join(cfg.CacheDir, "venvs"), cfg.WorkDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	script := filepath.Join(cfg.CacheDir, "envprep-"+ScriptVersion+".py")
	if err := os.WriteFile(script, envprepScript, 0o644); err != nil {
		return nil, err
	}
	return &Service{cfg: cfg, backend: backend, keys: map[string]*sync.Mutex{},
		sem: make(chan struct{}, cfg.MaxParallel), script: script}, nil
}

// Backend returns the backend's name (for logs).
func (s *Service) Backend() string { return s.backend.Name() }

// Register lets token run the job's roots; the runner calls it with the LLM token.
func (s *Service) Register(token string, job Job) { s.jobs.Store(token, job) }

// Unregister revokes a token.
func (s *Service) Unregister(token string) { s.jobs.Delete(token) }

func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	v, ok := s.jobs.Load(token)
	if token == "" || !ok {
		http.Error(w, "unknown token", http.StatusForbidden)
		return
	}
	job := v.(Job)
	var req Request
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil || len(body) > maxBody {
		http.Error(w, "request too large", http.StatusRequestEntityTooLarge)
		return
	}
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if err := validate(job, req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	resp := s.Run(r.Context(), job, req)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func validate(job Job, req Request) error {
	if strings.TrimSpace(req.Cmd) == "" {
		return fmt.Errorf("cmd is required")
	}
	if req.Root == "" {
		req.Root = "head"
	}
	if _, ok := job.Roots[req.Root]; !ok {
		labels := make([]string, 0, len(job.Roots))
		for k := range job.Roots {
			labels = append(labels, k)
		}
		return fmt.Errorf("unknown root %q (runnable: %s)", req.Root, strings.Join(labels, ", "))
	}
	for rel := range req.Files {
		if err := safeRel(rel); err != nil {
			return err
		}
	}
	return nil
}

func safeRel(rel string) error {
	clean := path.Clean(rel)
	if rel == "" || strings.HasPrefix(rel, "/") || strings.HasPrefix(rel, "~") || clean == "." ||
		clean == ".." || strings.HasPrefix(clean, "../") || strings.Contains(rel, "\\") {
		return fmt.Errorf("file path %q must be relative, inside the copy", rel)
	}
	return nil
}

func clamp(seconds int) time.Duration {
	d := time.Duration(seconds) * time.Second
	if seconds <= 0 {
		d = defaultTimeout
	}
	if d < 5*time.Second {
		d = 5 * time.Second
	}
	if d > maxTimeout {
		d = maxTimeout
	}
	return d
}

// Run prepares the root's venv (cached) and runs the command in a throwaway copy.
func (s *Service) Run(ctx context.Context, job Job, req Request) Response {
	if req.Root == "" {
		req.Root = "head"
	}
	if err := validate(job, req); err != nil {
		return Response{ExitCode: -1, Error: err.Error()}
	}
	src := job.Roots[req.Root]
	s.sem <- struct{}{}
	defer func() { <-s.sem }()

	env, venv := s.prepare(ctx, job, src)
	if !env.OK {
		resp := Response{ExitCode: -1, Env: env,
			Error: "env-prep failed for " + req.Root + ":\n" + strings.Join(env.Log, "\n")}
		s.log(job, req, resp)
		return resp
	}
	copyDir, err := os.MkdirTemp(s.cfg.WorkDir, "run-")
	if err != nil {
		return Response{ExitCode: -1, Env: env, Error: err.Error()}
	}
	defer os.RemoveAll(copyDir)
	work := filepath.Join(copyDir, "work")
	if err := copyTree(src, work); err != nil {
		return Response{ExitCode: -1, Env: env, Error: "copy: " + err.Error()}
	}
	for rel, text := range req.Files {
		target := filepath.Join(work, filepath.FromSlash(path.Clean(rel)))
		if err := os.MkdirAll(filepath.Dir(target), 0o777); err != nil {
			return Response{ExitCode: -1, Env: env, Error: err.Error()}
		}
		if err := os.WriteFile(target, []byte(text), 0o666); err != nil {
			return Response{ExitCode: -1, Env: env, Error: err.Error()}
		}
	}
	// The container runs as an unprivileged user: let it write its copy.
	_ = filepath.WalkDir(work, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.Type()&fs.ModeSymlink == 0 {
			if d.IsDir() {
				_ = os.Chmod(p, 0o777)
			} else {
				_ = os.Chmod(p, 0o666|modeExec(d))
			}
		}
		return nil
	})
	res, err := s.backend.Exec(ctx, ExecSpec{Work: work, Venv: venv, Cmd: req.Cmd,
		Timeout: clamp(req.TimeoutS), MemoryMB: s.cfg.MemoryMB, CPUs: s.cfg.CPUs})
	out, truncated := capOutput(string(res.Output))
	resp := Response{ExitCode: res.ExitCode, TimedOut: res.TimedOut, Seconds: res.Seconds,
		Output: out, Truncated: truncated, Env: env}
	if err != nil {
		resp.ExitCode, resp.Error = -1, err.Error()
	}
	s.log(job, req, resp)
	return resp
}

func modeExec(d fs.DirEntry) fs.FileMode {
	if info, err := d.Info(); err == nil && info.Mode()&0o111 != 0 {
		return 0o111
	}
	return 0
}

func (s *Service) lockFor(key string) *sync.Mutex {
	s.keyMu.Lock()
	defer s.keyMu.Unlock()
	if s.keys[key] == nil {
		s.keys[key] = &sync.Mutex{}
	}
	return s.keys[key]
}

// recipe returns the recipe file for the job: from base (the target branch) first, so
// an MR cannot change how it is itself tested, else from head.
func recipe(job Job) string {
	for _, label := range []string{"base", "head"} {
		if dir, ok := job.Roots[label]; ok {
			p := filepath.Join(dir, RecipeFile)
			if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() {
				return p
			}
		}
	}
	return ""
}

func (s *Service) prepare(ctx context.Context, job Job, src string) (Env, string) {
	recipePath := recipe(job)
	recipeText := ""
	if recipePath != "" {
		data, _ := os.ReadFile(recipePath)
		recipeText = string(data)
	}
	key, err := CacheKey(src, recipeText, s.backend.Tag())
	if err != nil {
		return Env{Log: []string{err.Error()}}, ""
	}
	venv := filepath.Join(s.cfg.CacheDir, "venvs", key)
	mu := s.lockFor(key)
	mu.Lock()
	defer mu.Unlock()
	if _, err := os.Stat(filepath.Join(venv, Marker)); err == nil {
		return Env{Key: key, OK: true, Cached: true}, venv
	}
	_ = os.RemoveAll(venv)
	if err := os.MkdirAll(venv, 0o755); err != nil {
		return Env{Key: key, Log: []string{err.Error()}}, ""
	}
	started := time.Now()
	out, err := s.backend.Prep(ctx, PrepSpec{Src: src, Venv: venv, Recipe: recipePath,
		Script: s.script, IndexURL: s.cfg.IndexURL, FindLinks: s.cfg.FindLinks,
		Timeout: s.cfg.PrepTimeout})
	_ = os.WriteFile(venv+".prep.log", out, 0o644)
	if err == nil {
		if _, statErr := os.Stat(filepath.Join(venv, Marker)); statErr != nil {
			err = fmt.Errorf("env-prep wrote no %s", Marker)
		}
	}
	if err != nil {
		_ = os.RemoveAll(venv)
		lines := strings.Split(strings.TrimSpace(string(out)), "\n")
		if len(lines) > 15 {
			lines = lines[len(lines)-15:]
		}
		slog.Warn("sandbox env-prep failed", "job", job.ID, "key", key, "error", err)
		return Env{Key: key, Log: append(lines, err.Error())}, ""
	}
	slog.Info("sandbox env-prep", "job", job.ID, "key", key,
		"seconds", time.Since(started).Round(time.Second).Seconds())
	return Env{Key: key, OK: true}, venv
}

func (s *Service) log(job Job, req Request, resp Response) {
	if job.TraceDir == "" {
		return
	}
	files := make([]string, 0, len(req.Files))
	for k := range req.Files {
		files = append(files, k)
	}
	rec, _ := json.Marshal(map[string]any{"ts": time.Now().Unix(), "job": job.ID,
		"root": req.Root, "cmd": req.Cmd, "files": files, "exit_code": resp.ExitCode,
		"timed_out": resp.TimedOut, "seconds": resp.Seconds, "env": resp.Env,
		"error": resp.Error, "output_bytes": len(resp.Output)})
	s.logMu.Lock()
	defer s.logMu.Unlock()
	f, err := os.OpenFile(filepath.Join(job.TraceDir, "sandbox.jsonl"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(rec, '\n'))
}

func capOutput(s string) (string, bool) {
	if len(s) <= maxOutput {
		return s, false
	}
	half := maxOutput / 2
	return s[:half] + "\n... output cut ...\n" + s[len(s)-half:], true
}

// copyTree copies src to dst, skipping VCS and cache directories. Symlinks are
// copied as links (never followed out of the tree).
func copyTree(src, dst string) error {
	return filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		if rel != "." && skipCopy[d.Name()] {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		target := filepath.Join(dst, rel)
		switch {
		case d.Type()&fs.ModeSymlink != 0:
			link, err := os.Readlink(p)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		case d.IsDir():
			return os.MkdirAll(target, 0o755)
		case d.Type().IsRegular():
			data, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			info, _ := d.Info()
			return os.WriteFile(target, data, info.Mode().Perm())
		}
		return nil
	})
}
