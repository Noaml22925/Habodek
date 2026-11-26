// Package config loads the control plane's settings from the environment.
//
// Reviewer v2: the control plane fetches code, queues work, runs the Python brain
// and posts its results. Everything about *how* to review lives in the brain
// (habodek/brain, models.yaml), not here.
package config

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	Server  ServerConfig
	Store   StoreConfig
	GitLab  GitLabConfig
	Git     GitConfig
	Worker  WorkerConfig
	Review  ReviewConfig
	Log     LogConfig
	Brain   BrainConfig
	Sandbox SandboxConfig
	Memory  MemoryConfig
	Bars    BarsConfig
}

// BarsConfig: automatic bar-raising from acceptance (M11, design §6.10).
type BarsConfig struct {
	Disabled      bool
	Window        int
	MinOutcomes   int
	MajorBelow    float64
	CriticalBelow float64
}

// MemoryConfig: the habodek-memory repository (M10). Dir "" or "none" disables memory.
type MemoryConfig struct {
	Dir          string
	MaxConsumers int // consumer repositories checked out for a library MR
	MapDebounce  int // minutes a default-branch push waits before re-mapping
}

type ServerConfig struct {
	Port string
	Host string
}

// StoreConfig: Driver "postgres" (production: the durable queue) or "sqlite".
type StoreConfig struct {
	Driver      string
	PostgresURL string
	SQLitePath  string
}

type GitLabConfig struct {
	BaseURL       string
	Token         string
	WebhookSecret string
	BotUserID     int64
}

type GitConfig struct {
	ReposDir string
}

type WorkerConfig struct {
	PoolSize int // brain jobs run at once (requests are the bottleneck, not CPU)
}

type ReviewConfig struct {
	TriggerLabel string // MR label that requests a review
}

type LogConfig struct {
	Level  string
	Format string
}

// BrainConfig configures the Python brain and the LLM proxy in front of the gateway.
// The brain never sees the gateway key or the GitLab token.
type BrainConfig struct {
	Command          string // habodek-brain executable
	WorkDir          string // per-job worktrees and job.json (disposable)
	TraceDir         string // per-job traces (kept)
	ProxyURL         string // how the brain reaches this server's LLM proxy
	GatewayURL       string // LiteLLM base URL
	GatewayKey       string // LiteLLM key
	GatewayRPM       float64
	TimeoutMinutes   int
	InternalPackages string // HABODEK_INTERNAL_PACKAGES for the brain
	PipIndexURL      string // Artifactory simple index for dependency source
	MaxComments      int
	ReplyDebounceSec int
}

// SandboxConfig: where the brain's `run` tool executes repository code (M9).
// Backend "docker" in production, "local" (unshare) on a dev box, "none" = no run tool.
type SandboxConfig struct {
	Backend     string
	URL         string // how the brain reaches this server's sandbox endpoint
	CacheDir    string // cached venvs, keyed by dependency files + recipe
	WorkDir     string // throwaway copies
	Image       string // runner image (python 3.12 + pytest)
	PrepNetwork string // docker network env-prep uses to reach Artifactory
	FindLinks   []string
	// PathMap translates this process's paths to the Docker host's when the control
	// plane itself runs in a container ("from=to,from=to"; longest prefix wins).
	PathMap     map[string]string
	MemoryMB    int
	CPUs        float64
	MaxParallel int
	PrepMinutes int
}

func Load() (*Config, error) {
	cfg := &Config{}
	cfg.Server.Port = envOrDefault("SERVER_PORT", "8080")
	cfg.Server.Host = envOrDefault("SERVER_HOST", "0.0.0.0")

	cfg.Store.Driver = strings.ToLower(envOrDefault("STORE_DRIVER", "sqlite"))
	switch cfg.Store.Driver {
	case "postgres":
		cfg.Store.PostgresURL = os.Getenv("DATABASE_URL")
		if cfg.Store.PostgresURL == "" {
			return nil, fmt.Errorf("DATABASE_URL is required when STORE_DRIVER=postgres")
		}
	case "sqlite", "file":
		// "file" was the v1 default; v2 keeps its state in SQLite instead.
		cfg.Store.Driver = "sqlite"
		cfg.Store.SQLitePath = envOrDefault("SQLITE_PATH", "data/habodek.db")
	default:
		return nil, fmt.Errorf("unsupported STORE_DRIVER: %s (must be postgres or sqlite)", cfg.Store.Driver)
	}

	cfg.GitLab.BaseURL = os.Getenv("GITLAB_BASE_URL")
	if cfg.GitLab.BaseURL == "" {
		return nil, fmt.Errorf("GITLAB_BASE_URL is required")
	}
	cfg.GitLab.Token = os.Getenv("GITLAB_TOKEN")
	if cfg.GitLab.Token == "" {
		return nil, fmt.Errorf("GITLAB_TOKEN is required")
	}
	cfg.GitLab.WebhookSecret = os.Getenv("GITLAB_WEBHOOK_SECRET")
	cfg.GitLab.BotUserID = int64(envIntOrDefault("GITLAB_BOT_USER_ID", 0))
	cfg.Git.ReposDir = envOrDefault("GIT_REPOS_DIR", "resource/repos")
	cfg.Worker.PoolSize = envIntOrDefault("WORKER_POOL_SIZE", 3)
	cfg.Review.TriggerLabel = envOrDefault("REVIEW_TRIGGER_LABEL", "ai-review")

	cfg.Brain.Command = envOrDefault("HABODEK_BRAIN_CMD", "habodek-brain")
	cfg.Brain.WorkDir = envOrDefault("HABODEK_WORK_DIR", "data/jobs")
	cfg.Brain.TraceDir = envOrDefault("HABODEK_TRACE_DIR", "data/traces")
	cfg.Brain.ProxyURL = envOrDefault("HABODEK_PROXY_URL",
		"http://127.0.0.1:"+cfg.Server.Port+"/internal/llm/v1")
	// OPENAI_BASE_URL / OPENAI_API_KEY are the v1 names for the LiteLLM gateway; the
	// deployment already sets them.
	cfg.Brain.GatewayURL = envOrDefault("HABODEK_GATEWAY_URL", os.Getenv("OPENAI_BASE_URL"))
	cfg.Brain.GatewayKey = envOrDefault("HABODEK_GATEWAY_KEY", os.Getenv("OPENAI_API_KEY"))
	cfg.Brain.GatewayRPM = envFloatOrDefault("HABODEK_GATEWAY_RPM", 25)
	cfg.Brain.TimeoutMinutes = envIntOrDefault("HABODEK_BRAIN_TIMEOUT_MIN", 90)
	cfg.Brain.InternalPackages = os.Getenv("HABODEK_INTERNAL_PACKAGES")
	cfg.Brain.PipIndexURL = os.Getenv("HABODEK_PIP_INDEX_URL")
	cfg.Brain.MaxComments = envIntOrDefault("HABODEK_MAX_COMMENTS", 6)
	cfg.Brain.ReplyDebounceSec = envIntOrDefault("HABODEK_REPLY_DEBOUNCE_SEC", 60)

	cfg.Sandbox.Backend = strings.ToLower(envOrDefault("HABODEK_SANDBOX", "none"))
	switch cfg.Sandbox.Backend {
	case "none", "local", "docker":
	default:
		return nil, fmt.Errorf("unsupported HABODEK_SANDBOX: %s (must be none, local or docker)", cfg.Sandbox.Backend)
	}
	cfg.Sandbox.URL = envOrDefault("HABODEK_SANDBOX_URL",
		"http://127.0.0.1:"+cfg.Server.Port+"/internal/sandbox/v1")
	cfg.Sandbox.CacheDir = envOrDefault("HABODEK_SANDBOX_CACHE", "data/sandbox")
	cfg.Sandbox.WorkDir = envOrDefault("HABODEK_SANDBOX_WORK", "data/sandbox-runs")
	cfg.Sandbox.Image = envOrDefault("HABODEK_SANDBOX_IMAGE", "habodek-runner:latest")
	cfg.Sandbox.PrepNetwork = envOrDefault("HABODEK_SANDBOX_PREP_NETWORK", "bridge")
	for _, p := range strings.Split(os.Getenv("HABODEK_SANDBOX_FIND_LINKS"), ",") {
		if p = strings.TrimSpace(p); p != "" {
			cfg.Sandbox.FindLinks = append(cfg.Sandbox.FindLinks, p)
		}
	}
	cfg.Sandbox.PathMap = map[string]string{}
	for _, pair := range strings.Split(os.Getenv("HABODEK_DOCKER_PATH_MAP"), ",") {
		from, to, ok := strings.Cut(strings.TrimSpace(pair), "=")
		if !ok || from == "" || to == "" {
			if strings.TrimSpace(pair) != "" {
				return nil, fmt.Errorf("HABODEK_DOCKER_PATH_MAP: %q is not from=to", pair)
			}
			continue
		}
		cfg.Sandbox.PathMap[filepath.Clean(from)] = filepath.Clean(to)
	}
	cfg.Sandbox.MemoryMB = envIntOrDefault("HABODEK_SANDBOX_MEMORY_MB", 4096)
	cfg.Sandbox.CPUs = envFloatOrDefault("HABODEK_SANDBOX_CPUS", 2)
	cfg.Sandbox.MaxParallel = envIntOrDefault("HABODEK_SANDBOX_PARALLEL", 2)
	cfg.Sandbox.PrepMinutes = envIntOrDefault("HABODEK_SANDBOX_PREP_MIN", 20)

	cfg.Memory.Dir = envOrDefault("HABODEK_MEMORY_DIR", "data/memory")
	if strings.EqualFold(cfg.Memory.Dir, "none") {
		cfg.Memory.Dir = ""
	}
	cfg.Memory.MaxConsumers = envIntOrDefault("HABODEK_MAX_CONSUMERS", 5)
	cfg.Memory.MapDebounce = envIntOrDefault("HABODEK_MAP_DEBOUNCE_MIN", 10)

	cfg.Bars.Disabled = strings.EqualFold(os.Getenv("HABODEK_BARS"), "off")
	cfg.Bars.Window = envIntOrDefault("HABODEK_BAR_WINDOW", 50)
	cfg.Bars.MinOutcomes = envIntOrDefault("HABODEK_BAR_MIN_OUTCOMES", 5)
	cfg.Bars.MajorBelow = envFloatOrDefault("HABODEK_BAR_MAJOR_BELOW", 0.5)
	cfg.Bars.CriticalBelow = envFloatOrDefault("HABODEK_BAR_CRITICAL_BELOW", 0.25)

	cfg.Log.Level = strings.ToLower(envOrDefault("LOG_LEVEL", "info"))
	cfg.Log.Format = strings.ToLower(envOrDefault("LOG_FORMAT", "json"))
	return cfg, nil
}

// ValidateForServer checks what only the webhook server needs.
func (c *Config) ValidateForServer() error {
	if c.GitLab.WebhookSecret == "" {
		return fmt.Errorf("GITLAB_WEBHOOK_SECRET is required")
	}
	if c.GitLab.BotUserID == 0 {
		return fmt.Errorf("GITLAB_BOT_USER_ID is required")
	}
	if c.Brain.GatewayURL == "" {
		return fmt.Errorf("HABODEK_GATEWAY_URL (or OPENAI_BASE_URL) is required")
	}
	return nil
}

// StoreDSN returns the driver and DSN for the orchestrator store.
func (c *Config) StoreDSN() (string, string) {
	if c.Store.Driver == "postgres" {
		return "postgres", c.Store.PostgresURL
	}
	return "sqlite3", c.Store.SQLitePath
}

func envOrDefault(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

func envIntOrDefault(key string, defaultVal int) int {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		slog.Warn("invalid int config, using default", "key", key, "value", v, "default", defaultVal)
		return defaultVal
	}
	return n
}

func envFloatOrDefault(key string, defaultVal float64) float64 {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		slog.Warn("invalid float config, using default", "key", key, "value", v, "default", defaultVal)
		return defaultVal
	}
	return f
}
