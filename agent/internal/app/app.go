// Package app wires the v2 control plane from configuration: store, GitLab client,
// git mirrors, the LLM proxy, the brain runner, the worker and the webhook triggers.
package app

import (
	"fmt"
	"os"
	"time"

	"habodek/internal/config"
	"habodek/internal/core/brain"
	"habodek/internal/orchestrator"
	"habodek/internal/pkg/git"
	"habodek/internal/pkg/gitlab"
	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/memory"
	"habodek/internal/pkg/sandbox"
)

type App struct {
	Config   *config.Config
	Store    *orchestrator.Store
	GitLab   *gitlab.Client
	Git      *git.Manager
	Proxy    *llmproxy.Proxy
	Sandbox  *sandbox.Service // nil when HABODEK_SANDBOX=none
	Memory   *memory.Store    // nil when HABODEK_MEMORY_DIR=none
	Runner   *brain.Runner
	Worker   *orchestrator.Worker
	Triggers *orchestrator.Triggers
}

// Build constructs everything; Close releases the store.
func Build(cfg *config.Config) (*App, error) {
	driver, dsn := cfg.StoreDSN()
	store, err := orchestrator.Open(driver, dsn)
	if err != nil {
		return nil, err
	}
	gl := gitlab.NewClient(cfg.GitLab.BaseURL, cfg.GitLab.Token)
	gm := git.NewManager(cfg.Git.ReposDir, cfg.GitLab.BaseURL, cfg.GitLab.Token)
	proxy := llmproxy.New(llmproxy.Config{UpstreamURL: cfg.Brain.GatewayURL,
		GatewayKey: cfg.Brain.GatewayKey, RPM: cfg.Brain.GatewayRPM})
	runner := brain.NewRunner(brain.RunnerConfig{Command: cfg.Brain.Command,
		WorkDir: cfg.Brain.WorkDir, TraceDir: cfg.Brain.TraceDir, ProxyURL: cfg.Brain.ProxyURL,
		Timeout:          time.Duration(cfg.Brain.TimeoutMinutes) * time.Minute,
		InternalPackages: cfg.Brain.InternalPackages, PipIndexURL: cfg.Brain.PipIndexURL,
		SandboxURL: cfg.Sandbox.URL}, proxy)
	sb, err := NewSandbox(cfg)
	if err != nil {
		store.Close()
		return nil, err
	}
	if sb != nil {
		runner.WithSandbox(sb)
	}
	worker := &orchestrator.Worker{Store: store, GitLab: gl, Poster: &orchestrator.Poster{GitLab: gl},
		Brain: runner, Workspaces: &brain.Workspaces{Git: gm, WorkDir: cfg.Brain.WorkDir}, Git: gm,
		Config: orchestrator.WorkerConfig{Workers: cfg.Worker.PoolSize, BotUserID: cfg.GitLab.BotUserID,
			MaxComments: cfg.Brain.MaxComments}}
	triggers := &orchestrator.Triggers{Store: store, WebhookSecret: cfg.GitLab.WebhookSecret,
		BotUserID: cfg.GitLab.BotUserID, TriggerLabel: cfg.Review.TriggerLabel,
		Debounce: time.Duration(cfg.Brain.ReplyDebounceSec) * time.Second}
	var mem *memory.Store
	if cfg.Memory.Dir != "" {
		if err := os.MkdirAll(cfg.Memory.Dir, 0o755); err != nil {
			store.Close()
			return nil, err
		}
		mem = memory.New(cfg.Memory.Dir)
		worker.Memory, worker.Applier = mem, runner
		worker.Config.MaxConsumers = cfg.Memory.MaxConsumers
		triggers.Memory = mem
		triggers.MapDebounce = time.Duration(cfg.Memory.MapDebounce) * time.Minute
	}
	return &App{Config: cfg, Store: store, GitLab: gl, Git: gm, Proxy: proxy, Sandbox: sb, Memory: mem,
		Runner: runner, Worker: worker, Triggers: triggers}, nil
}

// NewSandbox builds the sandbox service the configuration asks for (nil for "none").
func NewSandbox(cfg *config.Config) (*sandbox.Service, error) {
	var backend sandbox.Backend
	switch cfg.Sandbox.Backend {
	case "none", "":
		return nil, nil
	case "local":
		if !sandbox.LocalAvailable() {
			return nil, fmt.Errorf("HABODEK_SANDBOX=local needs unprivileged user namespaces (unshare -rn)")
		}
		backend = &sandbox.Local{}
	case "docker":
		backend = &sandbox.Docker{Image: cfg.Sandbox.Image, PrepNetwork: cfg.Sandbox.PrepNetwork}
	default:
		return nil, fmt.Errorf("unknown sandbox backend %q", cfg.Sandbox.Backend)
	}
	return sandbox.New(sandbox.Config{CacheDir: cfg.Sandbox.CacheDir, WorkDir: cfg.Sandbox.WorkDir,
		IndexURL: cfg.Brain.PipIndexURL, FindLinks: cfg.Sandbox.FindLinks,
		PrepTimeout: time.Duration(cfg.Sandbox.PrepMinutes) * time.Minute,
		MemoryMB:    cfg.Sandbox.MemoryMB, CPUs: cfg.Sandbox.CPUs,
		MaxParallel: cfg.Sandbox.MaxParallel}, backend)
}

func (a *App) Close() error { return a.Store.Close() }
