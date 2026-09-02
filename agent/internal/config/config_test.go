package config

import "testing"

func TestLoadAllowsCLIModesWithoutWebhookSettings(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
	t.Setenv("GITLAB_TOKEN", "token")
	t.Setenv("GITLAB_WEBHOOK_SECRET", "")
	t.Setenv("GITLAB_BOT_USER_ID", "")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	if err := cfg.ValidateForReview(); err == nil {
		t.Fatal("ValidateForReview() expected error, got nil")
	}
	if err := cfg.ValidateForServer(); err == nil {
		t.Fatal("ValidateForServer() expected error, got nil")
	}
}

func TestValidateForServerRequiresWebhookSettings(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
	t.Setenv("GITLAB_TOKEN", "token")
	t.Setenv("GITLAB_WEBHOOK_SECRET", "secret")
	t.Setenv("GITLAB_BOT_USER_ID", "42")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	if err := cfg.ValidateForReview(); err != nil {
		t.Fatalf("ValidateForReview() error = %v", err)
	}
	if err := cfg.ValidateForServer(); err != nil {
		t.Fatalf("ValidateForServer() error = %v", err)
	}
}

func TestContextWindowSizesMergesOverrides(t *testing.T) {
	sizes := contextWindowSizes("qwen3:8b=16384, qwen3.6=262144")

	if got := sizes["qwen3:8b"]; got != 16384 {
		t.Errorf(`sizes["qwen3:8b"] = %d, want 16384`, got)
	}
	if got := sizes["qwen3.6"]; got != 262144 {
		t.Errorf(`sizes["qwen3.6"] = %d, want 262144`, got)
	}
	if got := sizes["gpt-4o"]; got != 128000 {
		t.Errorf(`built-in entry lost: sizes["gpt-4o"] = %d, want 128000`, got)
	}
}

func TestContextWindowSizesOverridesBuiltIn(t *testing.T) {
	sizes := contextWindowSizes("gpt-4o=64000")

	if got := sizes["gpt-4o"]; got != 64000 {
		t.Errorf(`sizes["gpt-4o"] = %d, want 64000`, got)
	}
	if got := defaultContextWindows["gpt-4o"]; got != 128000 {
		t.Errorf("defaultContextWindows was mutated: gpt-4o = %d, want 128000", got)
	}
}

func TestContextWindowSizesSkipsMalformedEntries(t *testing.T) {
	for _, entry := range []string{"", "qwen3:8b", "=16384", "qwen3:8b=abc", "qwen3:8b=0", "qwen3:8b=-1"} {
		sizes := contextWindowSizes(entry)

		if _, ok := sizes["qwen3:8b"]; ok {
			t.Errorf("contextWindowSizes(%q) accepted a malformed entry", entry)
		}
		if len(sizes) != len(defaultContextWindows) {
			t.Errorf("contextWindowSizes(%q) len = %d, want %d", entry, len(sizes), len(defaultContextWindows))
		}
	}
}

func TestContextWindowSizesKeepsGoodEntriesBesideBadOnes(t *testing.T) {
	sizes := contextWindowSizes("garbage,qwen3:8b=16384")

	if got := sizes["qwen3:8b"]; got != 16384 {
		t.Errorf(`sizes["qwen3:8b"] = %d, want 16384`, got)
	}
}

// The seed has to stay absent unless it is asked for: a gateway that rejects an
// unknown parameter must not start failing because we added one.
func TestSeedIsUnsetUnlessConfigured(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
	t.Setenv("GITLAB_TOKEN", "token")
	t.Setenv("LLM_SEED", "")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.LLM.Seed != nil {
		t.Fatalf("LLM.Seed = %d, want nil when LLM_SEED is unset", *cfg.LLM.Seed)
	}
}

func TestSeedIsReadWhenConfigured(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
	t.Setenv("GITLAB_TOKEN", "token")
	t.Setenv("LLM_SEED", "1234")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.LLM.Seed == nil || *cfg.LLM.Seed != 1234 {
		t.Fatalf("LLM.Seed = %v, want 1234", cfg.LLM.Seed)
	}
}

// A typo must not take the agent down, and must not silently pin sampling to
// some arbitrary value either.
func TestMalformedSeedIsIgnored(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
	t.Setenv("GITLAB_TOKEN", "token")
	t.Setenv("LLM_SEED", "not-a-number")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.LLM.Seed != nil {
		t.Fatalf("LLM.Seed = %d, want nil for a malformed value", *cfg.LLM.Seed)
	}
}

func TestStructuredMaxTokensDefaultsAndOverrides(t *testing.T) {
	t.Setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
	t.Setenv("GITLAB_TOKEN", "token")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Review.StructuredMaxTokens != 8192 {
		t.Errorf("default StructuredMaxTokens = %d, want 8192", cfg.Review.StructuredMaxTokens)
	}

	t.Setenv("REVIEW_STRUCTURED_MAX_TOKENS", "32768")
	cfg, err = Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Review.StructuredMaxTokens != 32768 {
		t.Errorf("StructuredMaxTokens = %d, want 32768", cfg.Review.StructuredMaxTokens)
	}
}
