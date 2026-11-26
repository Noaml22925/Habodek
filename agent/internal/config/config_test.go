package config

import "testing"

func base(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example")
	t.Setenv("GITLAB_TOKEN", "t")
}

func TestLoadDefaultsForCLIWithoutWebhookSettings(t *testing.T) {
	base(t)
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if driver, dsn := cfg.StoreDSN(); driver != "sqlite3" || dsn != "data/habodek.db" {
		t.Fatalf("store = %s %s", driver, dsn)
	}
	if cfg.Brain.GatewayRPM != 25 || cfg.Brain.ProxyURL != "http://127.0.0.1:8080/internal/llm/v1" {
		t.Fatalf("brain = %+v", cfg.Brain)
	}
}

func TestV1GatewayNamesAreHonoured(t *testing.T) {
	base(t)
	t.Setenv("OPENAI_BASE_URL", "https://litellm.internal/v1")
	t.Setenv("OPENAI_API_KEY", "k")
	cfg, _ := Load()
	if cfg.Brain.GatewayURL != "https://litellm.internal/v1" || cfg.Brain.GatewayKey != "k" {
		t.Fatalf("gateway = %q %q", cfg.Brain.GatewayURL, cfg.Brain.GatewayKey)
	}
	t.Setenv("HABODEK_GATEWAY_URL", "https://other/v1")
	cfg, _ = Load()
	if cfg.Brain.GatewayURL != "https://other/v1" {
		t.Fatal("HABODEK_GATEWAY_URL must win")
	}
}

func TestValidateForServer(t *testing.T) {
	base(t)
	cfg, _ := Load()
	if cfg.ValidateForServer() == nil {
		t.Fatal("missing webhook settings must fail")
	}
	t.Setenv("GITLAB_WEBHOOK_SECRET", "s")
	t.Setenv("GITLAB_BOT_USER_ID", "9")
	t.Setenv("OPENAI_BASE_URL", "https://litellm/v1")
	cfg, _ = Load()
	if err := cfg.ValidateForServer(); err != nil {
		t.Fatal(err)
	}
}

func TestPostgresNeedsADatabaseURL(t *testing.T) {
	base(t)
	t.Setenv("STORE_DRIVER", "postgres")
	if _, err := Load(); err == nil {
		t.Fatal("expected an error")
	}
	t.Setenv("DATABASE_URL", "postgres://x")
	cfg, err := Load()
	if err != nil || cfg.Store.Driver != "postgres" {
		t.Fatalf("%v %+v", err, cfg.Store)
	}
}

func TestPostingPolicyAndPathMap(t *testing.T) {
	base(t)
	t.Setenv("HABODEK_MIN_SEVERITY", "major")
	t.Setenv("HABODEK_REPO_POLICY", `{"team/svc": {"min_severity": "critical", "max_comments": 3}}`)
	t.Setenv("HABODEK_DOCKER_PATH_MAP", "/var/lib/habodek=/srv/habodek")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Posting.MinSeverity != "major" || cfg.Posting.Repos["team/svc"].MaxComments != 3 ||
		cfg.Sandbox.PathMap["/var/lib/habodek"] != "/srv/habodek" {
		t.Fatalf("%+v %+v", cfg.Posting, cfg.Sandbox.PathMap)
	}
	for key, bad := range map[string]string{"HABODEK_MIN_SEVERITY": "low",
		"HABODEK_REPO_POLICY": `{"x": {"min_severity": "high"}}`, "HABODEK_DOCKER_PATH_MAP": "nope"} {
		t.Run(key, func(t *testing.T) {
			base(t)
			t.Setenv(key, bad)
			if _, err := Load(); err == nil {
				t.Fatalf("%s=%s accepted", key, bad)
			}
		})
	}
}
