package git

import (
	"bufio"
	"context"
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"log/slog"

	gogit "github.com/go-git/go-git/v5"
	gitconfig "github.com/go-git/go-git/v5/config"
	githttp "github.com/go-git/go-git/v5/plumbing/transport/http"
	"habodek/internal/domain"
)

const (
	gitLockTimeout  = 2 * time.Minute
	cloneMaxRetries = 5
	cloneRetryDelay = 15 * time.Second
)

// hunkHeaderRe matches unified diff hunk headers: @@ -old,count +new,count @@
var hunkHeaderRe = regexp.MustCompile(`^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@`)

type Manager struct {
	reposDir    string
	gitlabURL   string
	gitlabToken string
	lockMu      sync.Mutex
	gitLocks    map[int64]bool
}

func NewManager(reposDir, gitlabURL, gitlabToken string) *Manager {
	return &Manager{
		reposDir:    reposDir,
		gitlabURL:   gitlabURL,
		gitlabToken: gitlabToken,
		gitLocks:    make(map[int64]bool),
	}
}

func (m *Manager) RepoPath(projectID int64) string {
	return filepath.Join(m.reposDir, fmt.Sprintf("%d", projectID))
}

func (m *Manager) TargetBranchRef(branch string) string {
	return targetBranchRef(branch)
}

func (m *Manager) ReviewMRHeadRef(mrIID int64) string {
	return reviewMRHeadRef(mrIID)
}

// AcquireGitLock acquires an in-memory lock for git operations on a project.
func (m *Manager) AcquireGitLock(ctx context.Context, projectID int64) error {
	deadline := time.Now().Add(gitLockTimeout)
	backoff := 200 * time.Millisecond

	for time.Now().Before(deadline) {
		m.lockMu.Lock()
		if !m.gitLocks[projectID] {
			m.gitLocks[projectID] = true
			m.lockMu.Unlock()
			return nil
		}
		m.lockMu.Unlock()

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
		backoff = min(backoff*2, 5*time.Second)
	}
	return fmt.Errorf("git_lock_timeout for project %d", projectID)
}

// ReleaseGitLock releases the git lock.
func (m *Manager) ReleaseGitLock(_ context.Context, projectID int64) {
	m.lockMu.Lock()
	delete(m.gitLocks, projectID)
	m.lockMu.Unlock()
}

// FetchAndCheckout clones or fetches the repo, then ensures the given SHA exists locally.
// The repository is intentionally left without checkout so callers can read blobs by SHA
// without forcing full working tree materialization for large repositories.
func (m *Manager) FetchAndCheckout(ctx context.Context, projectID int64, projectPath string, mrIID int64, targetBranch, headSHA string) error {
	repoPath := m.RepoPath(projectID)
	cloneURL := fmt.Sprintf("%s/%s.git", m.gitlabURL, projectPath)

	cloned, err := m.ensureFullClone(ctx, repoPath, cloneURL)
	if err != nil {
		return fmt.Errorf("git clone: %w", err)
	}

	if !cloned {
		if err := m.fetchReviewRefsWithRetry(ctx, repoPath, targetBranch, mrIID); err != nil {
			return fmt.Errorf("git fetch failed after retry: %w", err)
		}
	}

	if err := m.runGit(ctx, repoPath, "cat-file", "-t", headSHA); err != nil {
		_ = m.fetchReviewRefsWithRetry(ctx, repoPath, targetBranch, mrIID)
		if err2 := m.runGit(ctx, repoPath, "cat-file", "-t", headSHA); err2 != nil {
			return fmt.Errorf("sha_not_found: %s", headSHA)
		}
	}

	return nil
}

func targetBranchRef(branch string) string {
	return "refs/remotes/origin/" + branch
}

func reviewMRHeadRef(mrIID int64) string {
	return fmt.Sprintf("refs/review-agent/mr/%d/head", mrIID)
}

func buildReviewFetchRefSpecs(targetBranch string, mrIID int64) []string {
	var refspecs []string
	if targetBranch != "" {
		refspecs = append(refspecs, fmt.Sprintf("+refs/heads/%s:%s", targetBranch, targetBranchRef(targetBranch)))
	}
	if mrIID > 0 {
		refspecs = append(refspecs, fmt.Sprintf("+refs/merge-requests/%d/head:%s", mrIID, reviewMRHeadRef(mrIID)))
	}
	return refspecs
}

func buildReviewGoGitRefSpecs(targetBranch string, mrIID int64) []gitconfig.RefSpec {
	stringSpecs := buildReviewFetchRefSpecs(targetBranch, mrIID)
	refspecs := make([]gitconfig.RefSpec, 0, len(stringSpecs))
	for _, spec := range stringSpecs {
		refspecs = append(refspecs, gitconfig.RefSpec(spec))
	}
	return refspecs
}

func (m *Manager) fetchReviewRefsWithRetry(ctx context.Context, repoPath, targetBranch string, mrIID int64) error {
	refspecs := buildReviewFetchRefSpecs(targetBranch, mrIID)
	if len(refspecs) == 0 {
		return fmt.Errorf("no fetch refspecs configured")
	}

	args := append([]string{"fetch", "origin", "--no-tags", "--filter=blob:none"}, refspecs...)

	return runWithFallback(ctx, "fetch review refs",
		retryPolicy{maxAttempts: 3, delay: 5 * time.Second, growth: 3, maxDelay: 45 * time.Second},
		gitAttempt{"go-git", func(ctx context.Context) error {
			return m.fetchReviewRefsWithGoGit(ctx, repoPath, targetBranch, mrIID)
		}},
		gitAttempt{"cli", func(ctx context.Context) error {
			return m.runGit(ctx, repoPath, args...)
		}},
	)
}

func (m *Manager) fetchReviewRefsWithGoGit(ctx context.Context, repoPath, targetBranch string, mrIID int64) error {
	repo, err := gogit.PlainOpen(repoPath)
	if err != nil {
		return err
	}

	refspecs := buildReviewGoGitRefSpecs(targetBranch, mrIID)
	if len(refspecs) == 0 {
		return fmt.Errorf("no fetch refspecs configured")
	}

	err = repo.FetchContext(ctx, &gogit.FetchOptions{
		RemoteName: "origin",
		Auth:       m.goGitAuth(),
		RefSpecs:   refspecs,
		Force:      true,
	})
	if err == gogit.NoErrAlreadyUpToDate {
		return nil
	}
	return err
}

func (m *Manager) ReadFileAtSHA(ctx context.Context, projectID int64, sha, filePath string) ([]byte, error) {
	repoPath := m.RepoPath(projectID)
	out, err := m.runGitOutput(ctx, repoPath, "show", fmt.Sprintf("%s:%s", sha, filePath))
	if err != nil {
		return nil, err
	}
	return []byte(out), nil
}

func (m *Manager) GrepAtSHA(ctx context.Context, projectID int64, sha, pattern string, args ...string) (string, error) {
	repoPath := m.RepoPath(projectID)
	gitArgs := []string{"grep"}
	gitArgs = append(gitArgs, args...)
	gitArgs = append(gitArgs, pattern, sha)
	return m.runGitOutput(ctx, repoPath, gitArgs...)
}

func (m *Manager) ListFilesAtSHA(ctx context.Context, projectID int64, sha, dir string) ([]string, error) {
	repoPath := m.RepoPath(projectID)
	args := []string{"ls-tree", "-r", "--name-only", sha}
	if dir != "" && dir != "." {
		args = append(args, dir)
	}
	out, err := m.runGitOutput(ctx, repoPath, args...)
	if err != nil {
		return nil, err
	}
	var files []string
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			files = append(files, line)
		}
	}
	return files, nil
}

func (m *Manager) GetFileSizeAtSHA(ctx context.Context, projectID int64, sha, filePath string) (int64, error) {
	repoPath := m.RepoPath(projectID)
	out, err := m.runGitOutput(ctx, repoPath, "cat-file", "-s", fmt.Sprintf("%s:%s", sha, filePath))
	if err != nil {
		return 0, err
	}
	size, convErr := strconv.ParseInt(strings.TrimSpace(out), 10, 64)
	if convErr != nil {
		return 0, convErr
	}
	return size, nil
}

func (m *Manager) FileExistsAtSHA(ctx context.Context, projectID int64, sha, filePath string) bool {
	repoPath := m.RepoPath(projectID)
	return m.runGit(ctx, repoPath, "cat-file", "-e", fmt.Sprintf("%s:%s", sha, filePath)) == nil
}

func (m *Manager) ensureFullClone(ctx context.Context, repoPath, cloneURL string) (bool, error) {
	reason := repoRecloneReason(repoPath)
	if reason == "" {
		return false, nil
	}

	if _, err := os.Stat(repoPath); err == nil {
		slog.Info("replacing repo cache with full clone",
			"path", repoPath,
			"reason", reason,
		)
		if err := os.RemoveAll(repoPath); err != nil {
			return false, fmt.Errorf("remove existing repo: %w", err)
		}
	}

	if err := m.cloneWithRetry(ctx, cloneURL, repoPath); err != nil {
		return false, err
	}
	return true, nil
}

func repoRecloneReason(repoPath string) string {
	gitDir := filepath.Join(repoPath, ".git")
	if _, err := os.Stat(gitDir); err != nil {
		return "missing_git_dir"
	}
	return ""
}

// IsAncestor checks if beforeSHA is an ancestor of headSHA (force-push detection).
func (m *Manager) IsAncestor(ctx context.Context, projectID int64, beforeSHA, headSHA string) (bool, error) {
	repoPath := m.RepoPath(projectID)
	err := m.runGit(ctx, repoPath, "merge-base", "--is-ancestor", beforeSHA, headSHA)
	if err != nil {
		return false, nil
	}
	return true, nil
}

// RevParse resolves a ref to a SHA.
func (m *Manager) RevParse(ctx context.Context, projectID int64, ref string) (string, error) {
	repoPath := m.RepoPath(projectID)
	out, err := m.runGitOutput(ctx, repoPath, "rev-parse", ref)
	if err != nil {
		return "", fmt.Errorf("rev-parse %s: %w", ref, err)
	}
	return strings.TrimSpace(out), nil
}

// SHAExists checks if a SHA exists in the repo.
func (m *Manager) SHAExists(ctx context.Context, projectID int64, sha string) bool {
	repoPath := m.RepoPath(projectID)
	return m.runGit(ctx, repoPath, "cat-file", "-t", sha) == nil
}

// Diff returns diff files between base and head.
func (m *Manager) Diff(ctx context.Context, projectID int64, baseSHA, headSHA string) ([]domain.DiffFile, error) {
	repoPath := m.RepoPath(projectID)

	nameStatus, err := m.runGitOutput(ctx, repoPath, "diff", "--name-status", baseSHA+".."+headSHA)
	if err != nil {
		return nil, fmt.Errorf("diff name-status: %w", err)
	}

	numstat, err := m.runGitOutput(ctx, repoPath, "diff", "--numstat", baseSHA+".."+headSHA)
	if err != nil {
		return nil, fmt.Errorf("diff numstat: %w", err)
	}

	statusMap := make(map[string]string)
	oldPathMap := make(map[string]string)
	for _, line := range strings.Split(strings.TrimSpace(nameStatus), "\n") {
		if line == "" {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) < 2 {
			continue
		}
		status := string(parts[0][0])
		path := parts[len(parts)-1]
		statusMap[path] = status
		if status == "R" && len(parts) >= 3 {
			oldPathMap[parts[2]] = parts[1]
		}
	}

	statMap := make(map[string][2]int)
	for _, line := range strings.Split(strings.TrimSpace(numstat), "\n") {
		if line == "" {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) < 3 {
			continue
		}
		added, _ := strconv.Atoi(parts[0])
		removed, _ := strconv.Atoi(parts[1])
		path := parts[2]
		statMap[path] = [2]int{added, removed}
	}

	var files []domain.DiffFile
	for path, status := range statusMap {
		stats := statMap[path]
		oldPath := path
		if op, ok := oldPathMap[path]; ok {
			oldPath = op
		}

		addedLines, _ := m.getAddedLines(ctx, repoPath, baseSHA, headSHA, path)

		files = append(files, domain.DiffFile{
			Path:         path,
			OldPath:      oldPath,
			Status:       status,
			LinesAdded:   stats[0],
			LinesRemoved: stats[1],
			AddedLines:   addedLines,
		})
	}

	return files, nil
}

// DiffFile returns the raw diff output for a single file between base and head.
func (m *Manager) DiffFile(ctx context.Context, projectID int64, baseSHA, headSHA, filePath string) (string, error) {
	repoPath := m.RepoPath(projectID)
	return m.runGitOutput(ctx, repoPath, "diff", baseSHA+".."+headSHA, "--", filePath)
}

// getAddedLines returns line numbers of added lines in a file diff.
func (m *Manager) getAddedLines(ctx context.Context, repoPath, baseSHA, headSHA, filePath string) ([]int, error) {
	out, err := m.runGitOutput(ctx, repoPath, "diff", "-U0", baseSHA+".."+headSHA, "--", filePath)
	if err != nil {
		return nil, err
	}

	var lines []int
	scanner := bufio.NewScanner(strings.NewReader(out))
	for scanner.Scan() {
		line := scanner.Text()
		matches := hunkHeaderRe.FindStringSubmatch(line)
		if matches == nil {
			continue
		}
		start, _ := strconv.Atoi(matches[1])
		count := 1
		if matches[2] != "" {
			count, _ = strconv.Atoi(matches[2])
		}
		for i := 0; i < count; i++ {
			lines = append(lines, start+i)
		}
	}
	return lines, nil
}

// CloneOrFetch clones the repository if absent, or fetches updates if it
// already exists locally. It is designed for bulk pre-warm operations; the
// caller should set an appropriate per-repo deadline on ctx.
func (m *Manager) CloneOrFetch(ctx context.Context, projectID int64, cloneURL string) (cloned bool, err error) {
	repoPath := m.RepoPath(projectID)

	cloned, err = m.ensureFullClone(ctx, repoPath, cloneURL)
	if err != nil {
		return false, fmt.Errorf("clone: %w", err)
	}
	if cloned {
		return true, nil
	}

	if _, statErr := os.Stat(filepath.Join(repoPath, ".git")); statErr == nil {
		if fetchErr := m.fetchWithRetry(ctx, repoPath); fetchErr != nil {
			return false, fmt.Errorf("fetch: %w", fetchErr)
		}
		return false, nil
	}
	return false, fmt.Errorf("clone: repository missing after clone preparation")
}

// gitAttempt is one way of reaching the remote, named so the logs say which
// transport actually did the work.
type gitAttempt struct {
	name string
	run  func(context.Context) error
}

type retryPolicy struct {
	maxAttempts int
	delay       time.Duration
	growth      int
	maxDelay    time.Duration
}

// runWithFallback retries primary with exponential backoff, then tries
// secondary once.
//
// go-git is primary for both clone and fetch. It authenticates with an
// explicit BasicAuth credential of its own, whereas the git CLI relies on the
// server's 401 handshake -- which on a corporate network can dead-end in a
// credential prompt that no longer has anywhere to go. The CLI stays as
// fallback because its --filter=blob:none partial clone is far cheaper on
// large repositories; go-git cannot filter blobs and fetches all of them.
func runWithFallback(ctx context.Context, op string, policy retryPolicy, primary, secondary gitAttempt) error {
	backoff := policy.delay
	var lastErr error

	for attempt := 1; attempt <= policy.maxAttempts; attempt++ {
		err := primary.run(ctx)
		if err == nil {
			return nil
		}
		lastErr = err
		slog.Warn("git "+op+" failed, retrying",
			"transport", primary.name,
			"attempt", attempt,
			"max_attempts", policy.maxAttempts,
			"error", err,
		)
		if attempt < policy.maxAttempts {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(backoff):
			}
			backoff = min(backoff*time.Duration(policy.growth), policy.maxDelay)
		}
	}

	slog.Warn("git "+op+" exhausted retries, falling back",
		"transport", primary.name,
		"fallback", secondary.name,
		"error", lastErr,
	)
	if err := secondary.run(ctx); err != nil {
		return fmt.Errorf("%s failed via %s (%v) and via %s: %w", op, primary.name, lastErr, secondary.name, err)
	}
	return nil
}

// fetchWithRetry runs broad git fetch for bulk prewarm operations.
func (m *Manager) fetchWithRetry(ctx context.Context, repoPath string) error {
	return runWithFallback(ctx, "fetch",
		retryPolicy{maxAttempts: 3, delay: 5 * time.Second, growth: 3, maxDelay: 45 * time.Second},
		gitAttempt{"go-git", func(ctx context.Context) error {
			return m.fetchWithGoGit(ctx, repoPath)
		}},
		gitAttempt{"cli", func(ctx context.Context) error {
			return m.runGit(ctx, repoPath, "fetch", "origin", "--prune")
		}},
	)
}

// cloneWithRetry clones a repo with retry logic and cleanup on failure.
func (m *Manager) cloneWithRetry(ctx context.Context, cloneURL, repoPath string) error {
	return runWithFallback(ctx, "clone",
		retryPolicy{maxAttempts: cloneMaxRetries, delay: cloneRetryDelay, growth: 2, maxDelay: 2 * time.Minute},
		gitAttempt{"go-git", func(ctx context.Context) error {
			return m.cloneWithGoGit(ctx, cloneURL, repoPath)
		}},
		gitAttempt{"cli", func(ctx context.Context) error {
			_ = os.RemoveAll(repoPath)
			return m.runGit(ctx, "", "clone", "--no-checkout", "--filter=blob:none", cloneURL, repoPath)
		}},
	)
}

func (m *Manager) cloneWithGoGit(ctx context.Context, cloneURL, repoPath string) error {
	_ = os.RemoveAll(repoPath)

	_, err := gogit.PlainCloneContext(ctx, repoPath, false, &gogit.CloneOptions{
		URL:        cloneURL,
		Auth:       m.goGitAuth(),
		NoCheckout: true,
	})
	if err != nil {
		_ = os.RemoveAll(repoPath)
		return err
	}
	return nil
}

func (m *Manager) fetchWithGoGit(ctx context.Context, repoPath string) error {
	repo, err := gogit.PlainOpen(repoPath)
	if err != nil {
		return err
	}

	err = repo.FetchContext(ctx, &gogit.FetchOptions{
		RemoteName: "origin",
		Auth:       m.goGitAuth(),
		RefSpecs:   []gitconfig.RefSpec{"+refs/heads/*:refs/remotes/origin/*"},
		Force:      true,
		Prune:      true,
	})
	if err == gogit.NoErrAlreadyUpToDate {
		return nil
	}
	return err
}

func (m *Manager) goGitAuth() *githttp.BasicAuth {
	if m.gitlabToken == "" {
		return nil
	}
	return &githttp.BasicAuth{
		Username: "oauth2",
		Password: m.gitlabToken,
	}
}

// GitEnv returns environment variables for git commands that inject the GitLab
// token, http buffer, and HTTP/1.1 settings via GIT_CONFIG environment variables.
//
// The token goes in as HTTP Basic, not as the PRIVATE-TOKEN header the REST API
// uses: GitLab's git-over-HTTP endpoint only accepts Basic, and answers anything
// else with a 401. Git responds to a 401 by asking the terminal for a username
// and password, which fails outright on a 2FA account and hangs forever in the
// worker. GIT_TERMINAL_PROMPT and the emptied credential.helper make that
// failure mode impossible rather than merely unlikely.
func (m *Manager) GitEnv() []string {
	env := append(os.Environ(),
		"GIT_TERMINAL_PROMPT=0",
		"GIT_ASKPASS=",
	)

	// Transport tuning shared by both branches. credential.helper is set to the
	// empty string to disable any system credential manager, which would
	// otherwise prompt, or cache and replay a stale credential.
	tuning := []string{
		"http.postBuffer", "524288000",
		"http.version", "HTTP/1.1",
		"http.lowSpeedLimit", "0",
		"http.lowSpeedTime", "0",
		"credential.helper", "",
	}

	var settings []string
	if m.gitlabToken != "" {
		basic := base64.StdEncoding.EncodeToString([]byte("oauth2:" + m.gitlabToken))
		settings = append(settings, "http.extraHeader", "Authorization: Basic "+basic)
	}
	settings = append(settings, tuning...)

	env = append(env, fmt.Sprintf("GIT_CONFIG_COUNT=%d", len(settings)/2))
	for i := 0; i < len(settings); i += 2 {
		n := i / 2
		env = append(env,
			fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", n, settings[i]),
			fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", n, settings[i+1]),
		)
	}
	return env
}

func (m *Manager) runGit(ctx context.Context, dir string, args ...string) error {
	cmd := exec.CommandContext(ctx, "git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	cmd.Env = m.GitEnv()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("git %s: %s: %w", strings.Join(args, " "), string(out), err)
	}
	return nil
}

func (m *Manager) runGitOutput(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	cmd.Env = m.GitEnv()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s: %s: %w", strings.Join(args, " "), string(out), err)
	}
	return string(out), nil
}
