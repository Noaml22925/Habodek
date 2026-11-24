package brain

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"habodek/internal/domain"
)

// GitOps is what the workspace builder needs from the git manager.
type GitOps interface {
	FetchAndCheckout(ctx context.Context, projectID int64, projectPath string, mrIID int64, targetBranch, headSHA string) error
	MergeBase(ctx context.Context, projectID int64, a, b string) (string, error)
	AddWorktree(ctx context.Context, projectID int64, sha, dir string) error
	RemoveWorktree(ctx context.Context, projectID int64, dir string) error
	SHAExists(ctx context.Context, projectID int64, sha string) bool
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
