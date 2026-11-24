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

func gitRepo(t *testing.T, dir, file, text string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	run(t, dir, "init", "-q", "-b", "main")
	_ = os.WriteFile(filepath.Join(dir, file), []byte(text), 0o644)
	run(t, dir, "add", "-A")
	run(t, dir, "commit", "-qm", "init")
	sha := run(t, dir, "rev-parse", "HEAD")
	run(t, dir, "update-ref", "refs/remotes/origin/main", sha)
	return sha
}

// The cartographer gets the default branch; a library review gets its consumers.
func TestPrepareBranchAndConsumers(t *testing.T) {
	root := t.TempDir()
	reposDir := filepath.Join(root, "repos")
	libSHA := gitRepo(t, filepath.Join(reposDir, "7"), "lib.py", "def f(): pass\n")
	gitRepo(t, filepath.Join(reposDir, "12"), "app.py", "from lib import f\n")
	g := &localGit{Manager: git.NewManager(reposDir, "http://gitlab.invalid", "")}
	ws := &Workspaces{Git: g, WorkDir: filepath.Join(root, "jobs")}

	p, err := ws.PrepareBranch(context.Background(), "map-1", 7, "team/lib", "main")
	if err != nil {
		t.Fatal(err)
	}
	if p.Revisions.HeadSHA != libSHA || p.Revisions.BaseSHA != libSHA || p.Workspace.Base != "" {
		t.Fatalf("map workspace %+v", p)
	}
	errs := ws.AddConsumers(context.Background(), p, "map-1", []Consumer{
		{ProjectID: 12, Path: "team/app", Branch: "main"},
		{ProjectID: 99, Path: "team/gone", Branch: "main"}, // no mirror: skipped, reported
	})
	if len(errs) != 1 || !strings.Contains(errs[0].Error(), "team/gone") {
		t.Fatalf("errors %v", errs)
	}
	text, _ := os.ReadFile(filepath.Join(p.Workspace.OtherRepos["app"], "app.py"))
	if string(text) != "from lib import f\n" || len(p.Workspace.OtherRepos) != 1 {
		t.Fatalf("consumer %+v %q", p.Workspace.OtherRepos, text)
	}
	p.Cleanup()
	if _, err := os.Stat(p.Workspace.OtherRepos["app"]); !os.IsNotExist(err) {
		t.Fatal("consumer worktree not removed")
	}
	if name := ConsumerName("other/app", map[string]string{"app": "x"}); name != "other-app" {
		t.Fatalf("clash name %q", name)
	}
}
