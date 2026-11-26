package orchestrator

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"habodek/internal/core/brain"
	"habodek/internal/domain"
	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/memory"
)

// BrainRunner runs one brain job (brain.Runner in production).
type BrainRunner interface {
	Run(ctx context.Context, job brain.Job) (*brain.Result, error)
}

// WorkspaceBuilder prepares worktrees for a job (brain.Workspaces in production).
type WorkspaceBuilder interface {
	Prepare(ctx context.Context, jobID string, projectID int64, projectPath string,
		mr *domain.GitLabMR, baseSHA string) (*brain.Prepared, error)
	PrepareBranch(ctx context.Context, jobID string, projectID int64, projectPath,
		branch string) (*brain.Prepared, error)
	AddConsumers(ctx context.Context, p *brain.Prepared, jobID string, consumers []brain.Consumer) []error
}

// MemoryApplier commits a finished job's memory updates (brain.Runner in production).
type MemoryApplier interface {
	ApplyMemory(ctx context.Context, repo, jobID string) (string, error)
}

func mapKey(projectID int64) string { return fmt.Sprintf("map:%d", projectID) }

// ChangedPaths lists files changed between two revisions of a project's mirror.
type ChangedPaths interface {
	ChangedPaths(ctx context.Context, projectID int64, from, to string) ([]string, error)
	FetchAndCheckout(ctx context.Context, projectID int64, projectPath string, mrIID int64, targetBranch, headSHA string) error
}

// WorkerConfig tunes the worker.
type WorkerConfig struct {
	Workers      int
	BotUserID    int64
	MaxComments  int
	Lease        time.Duration
	PollInterval time.Duration
	MaxAttempts  int
	MaxConsumers int // other projects checked out for a library MR (default 5)
	Bars         BarConfig
	// MinSeverity is the default posting floor; Repos override it and MaxComments
	// per project path.
	MinSeverity string
	Repos       map[string]RepoPolicy
}

// RepoPolicy overrides the posting policy for one project.
type RepoPolicy struct {
	MinSeverity string
	MaxComments int
}

// Worker claims tasks from the store and runs them.
type Worker struct {
	Store      *Store
	GitLab     domain.GitLabClient
	Poster     *Poster
	Brain      BrainRunner
	Workspaces WorkspaceBuilder
	Git        ChangedPaths
	Config     WorkerConfig
	// Memory is the habodek-memory repository (nil: no memory). Applier commits jobs'
	// memory updates into it.
	Memory  *memory.Store
	Applier MemoryApplier
}

// Start runs the worker goroutines until ctx ends; Wait blocks until they stop.
func (w *Worker) Start(ctx context.Context) *sync.WaitGroup {
	cfg := &w.Config
	if cfg.Workers <= 0 {
		cfg.Workers = 2
	}
	if cfg.Lease <= 0 {
		cfg.Lease = 5 * time.Minute
	}
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = time.Second
	}
	if cfg.MaxAttempts <= 0 {
		cfg.MaxAttempts = 3
	}
	var wg sync.WaitGroup
	for i := 0; i < cfg.Workers; i++ {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			w.loop(ctx, id)
		}(fmt.Sprintf("worker-%d", i))
	}
	return &wg
}

func (w *Worker) loop(ctx context.Context, id string) {
	for ctx.Err() == nil {
		if _, err := w.Store.RecoverExpired(ctx, w.Config.MaxAttempts); err != nil && ctx.Err() == nil {
			slog.Error("recover expired tasks", "error", err)
		}
		task, err := w.Store.Claim(ctx, id, w.Config.Lease)
		if err != nil || task == nil {
			if err != nil && ctx.Err() == nil {
				slog.Error("claim", "error", err)
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(w.Config.PollInterval):
			}
			continue
		}
		w.RunTask(ctx, task)
	}
}

// RunTask runs one claimed task to completion, keeping its lease alive.
func (w *Worker) RunTask(ctx context.Context, task *Task) {
	beat, stop := context.WithCancel(ctx)
	go func() {
		ticker := time.NewTicker(w.Config.Lease / 3)
		defer ticker.Stop()
		for {
			select {
			case <-beat.Done():
				return
			case <-ticker.C:
				_ = w.Store.Heartbeat(ctx, task.ID, w.Config.Lease)
			}
		}
	}()
	var err error
	switch task.Kind {
	case KindReview:
		err = w.review(ctx, task)
	case KindThread:
		err = w.thread(ctx, task)
	case KindPush:
		err = w.push(ctx, task)
	case KindMap:
		err = w.mapProject(ctx, task)
	default:
		err = fmt.Errorf("unknown task kind %q", task.Kind)
	}
	stop()
	status, msg := StatusDone, ""
	if err != nil {
		status, msg = StatusFailed, err.Error()
		slog.Error("task failed", "task", task.ID, "kind", task.Kind, "mr", task.MRIID, "error", err)
	}
	if ferr := w.Store.Finish(ctx, task.ID, status, msg); ferr != nil {
		slog.Error("finish task", "task", task.ID, "error", ferr)
	}
}

// withMemory points a job at the memory repository, pinned to its current commit.
func (w *Worker) withMemory(ctx context.Context, job *brain.Job) {
	if w.Memory == nil {
		return
	}
	dir, commit := w.Memory.Dir, w.Memory.Head(ctx)
	job.MemoryPath = &dir
	if commit != "" {
		job.MemoryCommit = &commit
	}
}

// applyMemory commits what a job asked to remember. A failure is logged, not fatal:
// the job's real output (the comment, the reply) has already been posted.
func (w *Worker) applyMemory(ctx context.Context, jobID string, result *brain.Result) {
	if w.Memory == nil || w.Applier == nil || len(result.MemoryUpdates) == 0 {
		return
	}
	commit, err := w.Memory.Write(func() (string, error) {
		return w.Applier.ApplyMemory(ctx, w.Memory.Dir, jobID)
	})
	if err != nil {
		slog.Error("memory update", "job", jobID, "error", err)
		return
	}
	slog.Info("memory updated", "job", jobID, "commit", commit, "updates", len(result.MemoryUpdates))
}

// enqueueMap asks the cartographer to map a project, in the background.
func (w *Worker) enqueueMap(ctx context.Context, projectID int64, projectPath, branch string) {
	if _, err := w.Store.Enqueue(ctx, Task{Kind: KindMap, ProjectID: projectID, ProjectPath: projectPath,
		ThreadKey: mapKey(projectID), Priority: llmproxy.PriorityBackground,
		Payload: map[string]any{"branch": branch}}); err != nil {
		slog.Warn("enqueue map", "project", projectPath, "error", err)
	}
}

// consumers of a library project, from the memory's reverse-dependency index.
func (w *Worker) consumers(projectPath string) []brain.Consumer {
	if w.Memory == nil {
		return nil
	}
	limit := w.Config.MaxConsumers
	if limit <= 0 {
		limit = 5
	}
	var out []brain.Consumer
	for _, f := range w.Memory.Consumers(projectPath) {
		if f.GitLabID == 0 || f.DefaultBranch == "" {
			continue // mapped without a GitLab id (a scenario run): cannot be fetched
		}
		out = append(out, brain.Consumer{ProjectID: f.GitLabID, Path: f.Project, Branch: f.DefaultBranch})
		if len(out) == limit {
			break
		}
	}
	return out
}

func (w *Worker) mapProject(ctx context.Context, task *Task) error {
	branch, _ := task.Payload["branch"].(string)
	if branch == "" {
		return fmt.Errorf("map task without a branch")
	}
	prepared, err := w.Workspaces.PrepareBranch(ctx, task.ID, task.ProjectID, task.ProjectPath, branch)
	if err != nil {
		return fmt.Errorf("workspace: %w", err)
	}
	defer prepared.Cleanup()
	job := brain.Job{Kind: "map", JobID: task.ID, Priority: "background",
		MR:        brain.MergeRequest{Project: task.ProjectPath, ProjectID: task.ProjectID, TargetBranch: branch},
		Revisions: prepared.Revisions, Workspace: prepared.Workspace}
	w.withMemory(ctx, &job)
	result, err := w.Brain.Run(ctx, job)
	if err != nil {
		return err
	}
	w.applyMemory(ctx, task.ID, result)
	return nil
}

func (w *Worker) mergeRequest(mr *domain.GitLabMR, projectID int64, projectPath string) brain.MergeRequest {
	return brain.MergeRequest{Project: projectPath, ProjectID: projectID, IID: mr.IID, Title: mr.Title,
		Description: mr.Description, Author: mr.AuthorUsername, Labels: mr.Labels,
		SourceBranch: mr.SourceBranch, TargetBranch: mr.TargetBranch, WebURL: mr.WebURL, Draft: mr.Draft}
}

func (w *Worker) review(ctx context.Context, task *Task) error {
	mr, err := w.GitLab.GetMR(ctx, task.ProjectID, task.MRIID)
	if err != nil {
		return fmt.Errorf("get MR: %w", err)
	}
	if mr.Draft {
		slog.Info("skipping draft MR", "mr", task.MRIID)
		return nil
	}
	note := "🔎 Habodek is reviewing this merge request. Findings appear as threads when the review " +
		"is done; nothing is posted until every finding has been verified."
	if err := w.Poster.Progress(ctx, task.ProjectID, task.MRIID, mr.HeadSHA, note); err != nil {
		slog.Warn("progress note", "error", err)
	}
	prepared, err := w.Workspaces.Prepare(ctx, task.ID, task.ProjectID, task.ProjectPath, mr, "")
	if err != nil {
		return fmt.Errorf("workspace: %w", err)
	}
	defer func() { prepared.Cleanup() }()
	if w.Memory != nil && !w.Memory.HasCard(task.ProjectPath) {
		// First review of this project: map it in the background for the next one.
		w.enqueueMap(ctx, task.ProjectID, task.ProjectPath, mr.TargetBranch)
	}
	if consumers := w.consumers(task.ProjectPath); len(consumers) > 0 {
		for _, cerr := range w.Workspaces.AddConsumers(ctx, prepared, task.ID, consumers) {
			slog.Warn("consumer worktree", "mr", task.MRIID, "error", cerr)
		}
	}
	open, err := w.Store.Findings(ctx, task.ProjectID, task.MRIID, true)
	if err != nil {
		return err
	}
	var openFindings []brain.Finding
	for _, f := range open {
		var bf brain.Finding
		if json.Unmarshal(f.FindingJSON, &bf) == nil {
			openFindings = append(openFindings, bf)
		}
	}
	job := brain.Job{Kind: "review", JobID: task.ID, Priority: "review",
		MR: w.mergeRequest(mr, task.ProjectID, task.ProjectPath), Revisions: prepared.Revisions,
		Workspace: prepared.Workspace, MaxComments: w.maxComments(task.ProjectPath),
		OpenFindings: openFindings, Policy: w.policy(ctx, task.ProjectID, task.ProjectPath)}
	w.withMemory(ctx, &job)
	result, err := w.Brain.Run(ctx, job)
	if err != nil {
		return err
	}
	if result.Status == "skipped" {
		return nil
	}
	posted, err := w.Poster.Review(ctx, task.ProjectID, task.MRIID, prepared.Revisions, result)
	if errors.Is(err, ErrHeadMoved) {
		// A newer head will be reviewed instead; nothing stale is posted.
		_, qerr := w.Store.Enqueue(ctx, Task{Kind: KindReview, ProjectID: task.ProjectID,
			ProjectPath: task.ProjectPath, MRIID: task.MRIID, ThreadKey: reviewKey(task.ProjectID, task.MRIID),
			Priority: llmproxy.PriorityReview})
		return qerr
	}
	byID := map[string]brain.Finding{}
	for _, f := range result.Posted() {
		byID[f.ID] = f
	}
	for _, p := range posted { // store what was posted, even if a later post failed
		f, ok := byID[p.FindingID]
		if !ok {
			continue
		}
		body, _ := json.Marshal(f)
		if serr := w.Store.SaveFinding(ctx, StoredFinding{ProjectID: task.ProjectID, MRIID: task.MRIID,
			FindingID: f.ID, DiscussionID: p.DiscussionID, NoteID: p.NoteID, HeadSHA: prepared.Revisions.HeadSHA,
			FindingJSON: body, ChainPaths: chainPaths(f)}); serr != nil {
			return serr
		}
	}
	return err
}

// chainPaths are the reviewed repository's files a finding's evidence runs through:
// a push touching any of them re-checks the finding.
func chainPaths(f brain.Finding) []string {
	seen := map[string]bool{f.Anchor.Path: true}
	paths := []string{f.Anchor.Path}
	for _, link := range f.Chain {
		if link.Repo != "" || seen[link.Path] {
			continue
		}
		seen[link.Path] = true
		paths = append(paths, link.Path)
	}
	return paths
}

func (w *Worker) thread(ctx context.Context, task *Task) error {
	discussionID, _ := task.Payload["discussion_id"].(string)
	stored, err := w.Store.FindingByDiscussion(ctx, task.ProjectID, task.MRIID, discussionID)
	if err != nil || stored == nil {
		return fmt.Errorf("finding for discussion %s: %v", discussionID, err)
	}
	var finding brain.Finding
	if err := json.Unmarshal(stored.FindingJSON, &finding); err != nil {
		return err
	}
	mr, err := w.GitLab.GetMR(ctx, task.ProjectID, task.MRIID)
	if err != nil {
		return err
	}
	discussion, err := w.GitLab.GetDiscussion(ctx, task.ProjectID, task.MRIID, discussionID)
	if err != nil {
		return err
	}
	var notes []brain.Note
	for _, n := range discussion.Notes {
		body := n.Body
		for _, m := range markersIn(body) {
			body = strings.ReplaceAll(body, m, "")
		}
		notes = append(notes, brain.Note{Author: n.AuthorName, Body: strings.TrimSpace(body),
			CreatedAt: n.CreatedAt.UTC().Format(time.RFC3339), IsBot: n.AuthorID == w.Config.BotUserID})
	}
	prepared, err := w.Workspaces.Prepare(ctx, task.ID, task.ProjectID, task.ProjectPath, mr, stored.HeadSHA)
	if err != nil {
		return fmt.Errorf("workspace: %w", err)
	}
	defer prepared.Cleanup()
	priority := "reply"
	if task.Event == "push" {
		priority = "recheck"
	}
	job := brain.Job{Kind: "thread", JobID: task.ID, Priority: priority, Event: task.Event,
		MR: w.mergeRequest(mr, task.ProjectID, task.ProjectPath), Revisions: prepared.Revisions,
		Workspace: prepared.Workspace, Finding: &finding, Notes: notes}
	w.withMemory(ctx, &job)
	result, err := w.Brain.Run(ctx, job)
	if err != nil {
		return err
	}
	if result.Thread == nil {
		return fmt.Errorf("thread job returned no verdict")
	}
	if err := w.Poster.Reply(ctx, task.ProjectID, task.MRIID, discussionID, task.ID, result.Thread.Body); err != nil {
		return err
	}
	lesson := ""
	if result.Thread.LessonCandidate != nil {
		lesson = *result.Thread.LessonCandidate
	}
	w.applyMemory(ctx, task.ID, result) // a verified withdrawal becomes (or reinforces) a lesson
	return w.Store.RecordVerdict(ctx, task.ProjectID, task.MRIID, stored.FindingID, result.Thread.Verdict, lesson)
}

// push re-checks every open finding whose evidence chain the new commits touch.
func (w *Worker) push(ctx context.Context, task *Task) error {
	open, err := w.Store.Findings(ctx, task.ProjectID, task.MRIID, true)
	if err != nil || len(open) == 0 {
		return err
	}
	mr, err := w.GitLab.GetMR(ctx, task.ProjectID, task.MRIID)
	if err != nil {
		return err
	}
	if err := w.Git.FetchAndCheckout(ctx, task.ProjectID, task.ProjectPath, mr.IID, mr.TargetBranch, mr.HeadSHA); err != nil {
		return err
	}
	for _, f := range open {
		changed, err := w.Git.ChangedPaths(ctx, task.ProjectID, f.HeadSHA, mr.HeadSHA)
		if err != nil {
			return err
		}
		if !intersects(changed, f.ChainPaths) {
			continue
		}
		if _, err := w.Store.Enqueue(ctx, Task{Kind: KindThread, ProjectID: task.ProjectID,
			ProjectPath: task.ProjectPath, MRIID: task.MRIID, Event: "push",
			ThreadKey: threadKey(task.ProjectID, task.MRIID, f.DiscussionID), Priority: llmproxy.PriorityRecheck,
			Payload: map[string]any{"finding_id": f.FindingID, "discussion_id": f.DiscussionID}}); err != nil {
			return err
		}
	}
	return nil
}

func intersects(a, b []string) bool {
	set := map[string]bool{}
	for _, x := range a {
		set[x] = true
	}
	for _, y := range b {
		if set[y] {
			return true
		}
	}
	return false
}
