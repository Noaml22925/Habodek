package brain

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"habodek/internal/domain"
	"habodek/internal/pkg/git"
)

// localGit is the real git.Manager over a repository that already exists locally
// (FetchAndCheckout is replaced: there is no GitLab in tests).
type localGit struct {
	*git.Manager
	fetched int
}

func (l *localGit) FetchAndCheckout(ctx context.Context, projectID int64, projectPath string, mrIID int64, targetBranch, headSHA string) error {
	l.fetched++
	return nil
}

func run(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@e",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@e")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %s", args, out)
	}
	return strings.TrimSpace(string(out))
}

func TestWorkspacesPrepareHeadAndBase(t *testing.T) {
	root := t.TempDir()
	reposDir := filepath.Join(root, "repos")
	repo := filepath.Join(reposDir, "7")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	run(t, repo, "init", "-q", "-b", "main")
	_ = os.WriteFile(filepath.Join(repo, "a.py"), []byte("x = 1\n"), 0o644)
	run(t, repo, "add", "-A")
	run(t, repo, "commit", "-qm", "base")
	base := run(t, repo, "rev-parse", "HEAD")
	run(t, repo, "checkout", "-qb", "feature")
	_ = os.WriteFile(filepath.Join(repo, "a.py"), []byte("x = 2\n"), 0o644)
	run(t, repo, "commit", "-qam", "change")
	head := run(t, repo, "rev-parse", "HEAD")
	run(t, repo, "checkout", "-q", "main")
	// The manager resolves the target branch as a remote-tracking ref.
	run(t, repo, "update-ref", "refs/remotes/origin/main", base)

	g := &localGit{Manager: git.NewManager(reposDir, "http://gitlab.invalid", "")}
	ws := &Workspaces{Git: g, WorkDir: filepath.Join(root, "jobs")}
	mr := &domain.GitLabMR{IID: 3, TargetBranch: "main", HeadSHA: head}
	prepared, err := ws.Prepare(context.Background(), "job-1", 7, "team/svc", mr, "")
	if err != nil {
		t.Fatal(err)
	}
	if prepared.Revisions.BaseSHA != base || g.fetched != 1 {
		t.Fatalf("revisions = %+v", prepared.Revisions)
	}
	headText, _ := os.ReadFile(filepath.Join(prepared.Workspace.Head, "a.py"))
	baseText, _ := os.ReadFile(filepath.Join(prepared.Workspace.Base, "a.py"))
	if string(headText) != "x = 2\n" || string(baseText) != "x = 1\n" {
		t.Fatalf("head %q base %q", headText, baseText)
	}
	prepared.Cleanup()
	if _, err := os.Stat(prepared.Workspace.Head); !os.IsNotExist(err) {
		t.Fatal("worktree not removed")
	}
	// A thread job uses the finding's SHA as base.
	again, err := ws.Prepare(context.Background(), "job-2", 7, "team/svc", mr, head)
	if err != nil || again.Revisions.BaseSHA != head {
		t.Fatalf("thread prepare: %v %+v", err, again)
	}
	again.Cleanup()
}
