package git

import (
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestRepoRecloneReason(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		setup  func(t *testing.T, repoPath string)
		reason string
	}{
		{
			name:   "missing git dir",
			setup:  func(_ *testing.T, _ string) {},
			reason: "missing_git_dir",
		},
		{
			name: "normal clone",
			setup: func(t *testing.T, repoPath string) {
				t.Helper()
				writeGitConfig(t, repoPath, `[remote "origin"]
	url = https://example.com/group/project.git
	fetch = +refs/heads/*:refs/remotes/origin/*
`)
			},
			reason: "",
		},
	}

	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()

			repoPath := t.TempDir()
			tt.setup(t, repoPath)

			if got := repoRecloneReason(repoPath); got != tt.reason {
				t.Fatalf("repoRecloneReason() = %q, want %q", got, tt.reason)
			}
		})
	}
}

func writeGitConfig(t *testing.T, repoPath, config string) {
	t.Helper()

	gitDir := filepath.Join(repoPath, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		t.Fatalf("MkdirAll(%q): %v", gitDir, err)
	}
	if err := os.WriteFile(filepath.Join(gitDir, "config"), []byte(config), 0o644); err != nil {
		t.Fatalf("WriteFile(config): %v", err)
	}
}

// gitConfigFromEnv rebuilds the git config that GitEnv encodes into
// GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n, so assertions read as config rather
// than as string positions in an env slice.
func gitConfigFromEnv(t *testing.T, env []string) map[string]string {
	t.Helper()

	raw := map[string]string{}
	for _, e := range env {
		k, v, ok := strings.Cut(e, "=")
		if ok {
			raw[k] = v
		}
	}

	count, err := strconv.Atoi(raw["GIT_CONFIG_COUNT"])
	if err != nil {
		t.Fatalf("GIT_CONFIG_COUNT not an integer: %q", raw["GIT_CONFIG_COUNT"])
	}

	cfg := map[string]string{}
	for i := range count {
		key, ok := raw[fmt.Sprintf("GIT_CONFIG_KEY_%d", i)]
		if !ok {
			t.Fatalf("GIT_CONFIG_COUNT is %d but GIT_CONFIG_KEY_%d is missing", count, i)
		}
		cfg[key] = raw[fmt.Sprintf("GIT_CONFIG_VALUE_%d", i)]
	}
	return cfg
}

func TestGitEnvAuthenticatesWithBasic(t *testing.T) {
	m := NewManager(t.TempDir(), "https://gitlab.example.com", "glpat-secret")

	cfg := gitConfigFromEnv(t, m.GitEnv())

	// GitLab's git-over-HTTP endpoint accepts Basic and nothing else. The
	// PRIVATE-TOKEN header is for the REST API; sending it here earns a 401.
	want := "Authorization: Basic " + base64.StdEncoding.EncodeToString([]byte("oauth2:glpat-secret"))
	if got := cfg["http.extraHeader"]; got != want {
		t.Errorf("http.extraHeader = %q, want %q", got, want)
	}
	if strings.Contains(cfg["http.extraHeader"], "PRIVATE-TOKEN") {
		t.Error("http.extraHeader still sends PRIVATE-TOKEN, which git transport rejects")
	}
}

func TestGitEnvNeverPrompts(t *testing.T) {
	// A 401 must fail the command, not stop to ask a human for a password:
	// interactively that dead-ends on a 2FA account, and in the worker it hangs
	// forever with no output.
	for _, token := range []string{"glpat-secret", ""} {
		env := NewManager(t.TempDir(), "https://gitlab.example.com", token).GitEnv()

		var terminalPrompt string
		for _, e := range env {
			if v, ok := strings.CutPrefix(e, "GIT_TERMINAL_PROMPT="); ok {
				terminalPrompt = v
			}
		}
		if terminalPrompt != "0" {
			t.Errorf("token %q: GIT_TERMINAL_PROMPT = %q, want \"0\"", token, terminalPrompt)
		}

		cfg := gitConfigFromEnv(t, env)
		if helper, ok := cfg["credential.helper"]; !ok || helper != "" {
			t.Errorf("token %q: credential.helper = %q (present=%v), want empty and present", token, helper, ok)
		}
	}
}

func TestGitEnvOmitsAuthHeaderWithoutToken(t *testing.T) {
	cfg := gitConfigFromEnv(t, NewManager(t.TempDir(), "https://gitlab.example.com", "").GitEnv())

	if _, ok := cfg["http.extraHeader"]; ok {
		t.Error("http.extraHeader set despite there being no token")
	}
	if cfg["http.version"] != "HTTP/1.1" {
		t.Errorf("http.version = %q, want HTTP/1.1", cfg["http.version"])
	}
}
