package brain

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"habodek/internal/domain"
)

// GitOps is what the workspace builder needs from the git manager.
type GitOps interface {
	FetchAndCheckout(ctx context.Context, projectID int64, projectPath string, mrIID int64, targetBranch, headSHA string) error
	MergeBase(ctx context.Context, projectID int64, a, b string) (string, error)
	AddWorktree(ctx context.Context, projectID int64, sha, dir string) error
	RemoveWorktree(ctx context.Context, projectID int64, dir string) error
	SHAExists(ctx context.Context, projectID int64, sha string) bool
	RevParse(ctx context.Context, projectID int64, ref string) (string, error)
	RepoPath(projectID int64) string
	TargetBranchRef(branch string) string
	AcquireGitLock(ctx context.Context, projectID int64) error
	ReleaseGitLock(ctx context.Context, projectID int64)
}

// Prepared is a job's workspace on disk, with a cleanup that removes the worktrees.
type Prepared struct {
	Workspace Workspace
	Revisions Revisions
	Cleanup   func()
}

// Workspaces builds the worktrees the brain reads (design §6.3): head and merge base of
// the MR, from the project's mirror.
type Workspaces struct {
	Git     GitOps
	WorkDir string
}

// Prepare fetches the MR into the mirror and creates worktrees for head and base.
// For a review, base is GitLab's diff_refs.base_sha (falling back to the merge base
// with the target branch). For a thread job, baseSHA is the SHA the finding was made
// at, so the brain's change map is diff(finding_sha, HEAD).
func (w *Workspaces) Prepare(ctx context.Context, jobID string, projectID int64, projectPath string,
	mr *domain.GitLabMR, baseSHA string) (*Prepared, error) {
	if err := w.Git.AcquireGitLock(ctx, projectID); err != nil {
		return nil, err
	}
	defer w.Git.ReleaseGitLock(ctx, projectID)
	if err := w.Git.FetchAndCheckout(ctx, projectID, projectPath, mr.IID, mr.TargetBranch, mr.HeadSHA); err != nil {
		return nil, err
	}
	if baseSHA == "" {
		baseSHA = mr.DiffRefs.BaseSHA
	}
	if baseSHA == "" || !w.Git.SHAExists(ctx, projectID, baseSHA) {
		mb, err := w.Git.MergeBase(ctx, projectID, w.Git.TargetBranchRef(mr.TargetBranch), mr.HeadSHA)
		if err != nil {
			return nil, fmt.Errorf("merge base: %w", err)
		}
		baseSHA = mb
	}
	dir := filepath.Join(w.WorkDir, jobID)
	head, base := filepath.Join(dir, "head"), filepath.Join(dir, "base")
	var created []string
	cleanup := func() {
		for _, d := range created {
			_ = w.Git.RemoveWorktree(context.Background(), projectID, d)
		}
		_ = os.RemoveAll(dir)
	}
	for _, wt := range []struct{ dir, sha string }{{head, mr.HeadSHA}, {base, baseSHA}} {
		if err := w.Git.AddWorktree(ctx, projectID, wt.sha, wt.dir); err != nil {
			cleanup()
			return nil, fmt.Errorf("worktree %s: %w", wt.sha, err)
		}
		created = append(created, wt.dir)
	}
	return &Prepared{
		Workspace: Workspace{Head: head, Base: base, Repo: w.Git.RepoPath(projectID)},
		Revisions: Revisions{BaseSHA: baseSHA, HeadSHA: mr.HeadSHA, StartSHA: mr.DiffRefs.StartSHA},
		Cleanup:   cleanup,
	}, nil
}

// PrepareBranch is the cartographer's workspace: a worktree of the project's branch
// (its default branch) at its current head. Base and head are the same revision.
func (w *Workspaces) PrepareBranch(ctx context.Context, jobID string, projectID int64, projectPath,
	branch string) (*Prepared, error) {
	if err := w.Git.AcquireGitLock(ctx, projectID); err != nil {
		return nil, err
	}
	defer w.Git.ReleaseGitLock(ctx, projectID)
	ref := w.Git.TargetBranchRef(branch)
	if err := w.Git.FetchAndCheckout(ctx, projectID, projectPath, 0, branch, ref); err != nil {
		return nil, err
	}
	sha, err := w.Git.RevParse(ctx, projectID, ref)
	if err != nil {
		return nil, fmt.Errorf("resolve %s: %w", branch, err)
	}
	dir := filepath.Join(w.WorkDir, jobID)
	head := filepath.Join(dir, "head")
	if err := w.Git.AddWorktree(ctx, projectID, sha, head); err != nil {
		_ = os.RemoveAll(dir)
		return nil, fmt.Errorf("worktree %s: %w", sha, err)
	}
	return &Prepared{
		Workspace: Workspace{Head: head, Repo: w.Git.RepoPath(projectID)},
		Revisions: Revisions{BaseSHA: sha, HeadSHA: sha},
		Cleanup: func() {
			_ = w.Git.RemoveWorktree(context.Background(), projectID, head)
			_ = os.RemoveAll(dir)
		},
	}, nil
}

// Consumer is another project that depends on the reviewed one (from memory).
type Consumer struct {
	ProjectID int64
	Path      string // path_with_namespace
	Branch    string // its default branch
}

// ConsumerName is the label a consumer gets in the job: `repo:<name>`.
func ConsumerName(path string, taken map[string]string) string {
	name := path[strings.LastIndex(path, "/")+1:]
	if _, clash := taken[name]; clash {
		name = strings.ReplaceAll(path, "/", "-")
	}
	return name
}

// AddConsumers adds a worktree of each consumer's default branch to a prepared job
// (§3.5 step 4: a library MR is reviewed against the code that uses the library).
// A consumer that cannot be fetched is skipped and reported, not fatal.
func (w *Workspaces) AddConsumers(ctx context.Context, p *Prepared, jobID string,
	consumers []Consumer) []error {
	var errs []error
	for _, c := range consumers {
		if p.Workspace.OtherRepos == nil {
			p.Workspace.OtherRepos = map[string]string{}
		}
		name := ConsumerName(c.Path, p.Workspace.OtherRepos)
		dir := filepath.Join(w.WorkDir, jobID, "repos", name)
		if err := w.addConsumer(ctx, c, dir); err != nil {
			errs = append(errs, fmt.Errorf("consumer %s: %w", c.Path, err))
			continue
		}
		p.Workspace.OtherRepos[name] = dir
		prev, pid := p.Cleanup, c.ProjectID
		p.Cleanup = func() {
			_ = w.Git.RemoveWorktree(context.Background(), pid, dir)
			prev()
		}
	}
	return errs
}

func (w *Workspaces) addConsumer(ctx context.Context, c Consumer, dir string) error {
	if err := w.Git.AcquireGitLock(ctx, c.ProjectID); err != nil {
		return err
	}
	defer w.Git.ReleaseGitLock(ctx, c.ProjectID)
	ref := w.Git.TargetBranchRef(c.Branch)
	if err := w.Git.FetchAndCheckout(ctx, c.ProjectID, c.Path, 0, c.Branch, ref); err != nil {
		return err
	}
	sha, err := w.Git.RevParse(ctx, c.ProjectID, ref)
	if err != nil {
		return err
	}
	return w.Git.AddWorktree(ctx, c.ProjectID, sha, dir)
}
