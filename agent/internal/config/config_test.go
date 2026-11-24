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
