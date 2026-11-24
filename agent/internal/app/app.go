// Package app wires the v2 control plane from configuration: store, GitLab client,
// git mirrors, the LLM proxy, the brain runner, the worker and the webhook triggers.
package app

import (
	"time"

	"habodek/internal/config"
	"habodek/internal/core/brain"
	"habodek/internal/orchestrator"
	"habodek/internal/pkg/git"
	"habodek/internal/pkg/gitlab"
	"habodek/internal/pkg/llmproxy"
)

type App struct {
	Config   *config.Config
	Store    *orchestrator.Store
	GitLab   *gitlab.Client
	Git      *git.Manager
	Proxy    *llmproxy.Proxy
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
		InternalPackages: cfg.Brain.InternalPackages, PipIndexURL: cfg.Brain.PipIndexURL}, proxy)
	worker := &orchestrator.Worker{Store: store, GitLab: gl, Poster: &orchestrator.Poster{GitLab: gl},
		Brain: runner, Workspaces: &brain.Workspaces{Git: gm, WorkDir: cfg.Brain.WorkDir}, Git: gm,
		Config: orchestrator.WorkerConfig{Workers: cfg.Worker.PoolSize, BotUserID: cfg.GitLab.BotUserID,
			MaxComments: cfg.Brain.MaxComments}}
	triggers := &orchestrator.Triggers{Store: store, WebhookSecret: cfg.GitLab.WebhookSecret,
		BotUserID: cfg.GitLab.BotUserID, TriggerLabel: cfg.Review.TriggerLabel,
		Debounce: time.Duration(cfg.Brain.ReplyDebounceSec) * time.Second}
	return &App{Config: cfg, Store: store, GitLab: gl, Git: gm, Proxy: proxy, Runner: runner,
		Worker: worker, Triggers: triggers}, nil
}

func (a *App) Close() error { return a.Store.Close() }
