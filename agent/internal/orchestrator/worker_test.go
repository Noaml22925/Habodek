package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"habodek/internal/core/brain"
	"habodek/internal/domain"
	"habodek/internal/pkg/gitlab"
	"habodek/internal/testutil/fakegitlab"
)

const botID = 99

type cannedBrain struct {
	mu   sync.Mutex
	jobs []brain.Job
}

func (b *cannedBrain) Run(_ context.Context, job brain.Job) (*brain.Result, error) {
	b.mu.Lock()
	b.jobs = append(b.jobs, job)
	b.mu.Unlock()
	if job.Kind == "map" {
		return &brain.Result{Contract: 1, JobID: job.JobID, Kind: "map", Status: "ok",
			MemoryUpdates: []brain.MemoryUpdate{{Kind: "card", Project: job.MR.Project, Content: "# card"}}}, nil
	}
	if job.Kind == "thread" {
		lesson := "middleware covers v1"
		return &brain.Result{Contract: 1, JobID: job.JobID, Kind: "thread", Status: "ok",
			Thread: &brain.ThreadVerdict{Verdict: "withdrawn", Body: "You're right, withdrawn.",
				LessonCandidate: &lesson},
			MemoryUpdates: []brain.MemoryUpdate{{Kind: "lesson", Project: job.MR.Project,
				Lesson: &brain.LessonEvent{Text: lesson}}}}, nil
	}
	finding := brain.Finding{ID: "f1", Title: "t", Claim: "c", Status: "posted",
		Anchor: brain.Anchor{Path: "store/users.py", Line: 28, Side: "new", Snippet: "return None"},
		Chain: []brain.EvidenceLink{{Path: "store/users.py", Line: 28, Quote: "return None", Claim: "x"},
			{Path: "cache/users.py", Line: 27, Quote: "except NotFound:", Claim: "y"},
			{Path: "orders/api/profile.py", Line: 17, Quote: "user.email", Claim: "z", Repo: "repo:orders-service"}}}
	return &brain.Result{Contract: 1, JobID: job.JobID, Kind: "review", Status: "ok",
		Summary: "One issue found.", Findings: []brain.Finding{finding},
		Comments: []brain.Comment{{FindingID: "f1", Path: "store/users.py", Line: 28, Side: "new",
			Body: "🟠 get_user returns None\n\n<!-- habodek:finding:f1 -->", Marker: "<!-- habodek:finding:f1 -->"}}}, nil
}

type fakeWorkspaces struct {
	bases     []string
	branches  []string
	consumers []brain.Consumer
}

func (f *fakeWorkspaces) Prepare(_ context.Context, jobID string, _ int64, _ string, mr *domain.GitLabMR,
	baseSHA string) (*brain.Prepared, error) {
	if baseSHA == "" {
		baseSHA = mr.DiffRefs.BaseSHA
	}
	f.bases = append(f.bases, baseSHA)
	return &brain.Prepared{Workspace: brain.Workspace{Head: "/w/" + jobID},
		Revisions: brain.Revisions{BaseSHA: baseSHA, HeadSHA: mr.HeadSHA, StartSHA: mr.DiffRefs.StartSHA},
		Cleanup:   func() {}}, nil
}

func (f *fakeWorkspaces) PrepareBranch(_ context.Context, jobID string, _ int64, _ string,
	branch string) (*brain.Prepared, error) {
	f.branches = append(f.branches, branch)
	return &brain.Prepared{Workspace: brain.Workspace{Head: "/w/" + jobID},
		Revisions: brain.Revisions{BaseSHA: "main1", HeadSHA: "main1"}, Cleanup: func() {}}, nil
}

func (f *fakeWorkspaces) AddConsumers(_ context.Context, p *brain.Prepared, _ string,
	consumers []brain.Consumer) []error {
	for _, c := range consumers {
		if p.Workspace.OtherRepos == nil {
			p.Workspace.OtherRepos = map[string]string{}
		}
		f.consumers = append(f.consumers, c)
		p.Workspace.OtherRepos[brain.ConsumerName(c.Path, p.Workspace.OtherRepos)] = "/w/" + c.Path
	}
	return nil
}

type fakeGit struct{ changed []string }

func (f *fakeGit) ChangedPaths(context.Context, int64, string, string) ([]string, error) {
	return f.changed, nil
}
func (f *fakeGit) FetchAndCheckout(context.Context, int64, string, int64, string, string) error {
	return nil
}

type rig struct {
	store    *Store
	gl       *fakegitlab.GitLab
	brain    *cannedBrain
	ws       *fakeWorkspaces
	git      *fakeGit
	worker   *Worker
	triggers *Triggers
	hook     *httptest.Server
}

func newRig(t *testing.T) *rig {
	t.Helper()
	store, err := Open("sqlite3", filepath.Join(t.TempDir(), "hbk.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	gl := fakegitlab.New(botID)
	t.Cleanup(gl.Close)
	gl.MRs[7] = &fakegitlab.MR{IID: 7, Title: "Return None", TargetBranch: "main", HeadSHA: "head1",
		BaseSHA: "base1", StartSHA: "start1", Labels: []string{"ai-review"}, Author: "dev"}
	client := gitlab.NewClient(gl.URL(), "token")
	r := &rig{store: store, gl: gl, brain: &cannedBrain{}, ws: &fakeWorkspaces{}, git: &fakeGit{}}
	r.worker = &Worker{Store: store, GitLab: client, Poster: &Poster{GitLab: client}, Brain: r.brain,
		Workspaces: r.ws, Git: r.git, Config: WorkerConfig{BotUserID: botID, MaxComments: 6,
			Lease: time.Minute, PollInterval: 10 * time.Millisecond}}
	r.triggers = &Triggers{Store: store, WebhookSecret: "s3cret", BotUserID: botID, TriggerLabel: "ai-review",
		Debounce: 50 * time.Millisecond}
	r.hook = httptest.NewServer(r.triggers)
	t.Cleanup(r.hook.Close)
	return r
}

func (r *rig) webhook(t *testing.T, kind string, payload map[string]any) {
	t.Helper()
	payload["object_kind"] = kind
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequest(http.MethodPost, r.hook.URL, strings.NewReader(string(body)))
	req.Header.Set("X-Gitlab-Token", "s3cret")
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("webhook %s: %v %v", kind, err, resp.Status)
	}
	resp.Body.Close()
}

// drain runs every ready task until the queue is empty (waiting out debounces).
func (r *rig) drain(t *testing.T) int {
	t.Helper()
	ran := 0
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		task, err := r.store.Claim(context.Background(), "test", time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		if task == nil {
			var queued int
			_ = r.store.db.QueryRow(`SELECT COUNT(*) FROM hbk_tasks WHERE status = 'queued'`).Scan(&queued)
			if queued == 0 {
				return ran
			}
			time.Sleep(20 * time.Millisecond)
			continue
		}
		r.worker.RunTask(context.Background(), task)
		got, _ := r.store.Get(context.Background(), task.ID)
		if got.Status != StatusDone {
			t.Fatalf("task %s (%s) %s: %s", task.ID, task.Kind, got.Status, got.Error)
		}
		ran++
	}
	t.Fatal("queue did not drain")
	return ran
}

func labelEvent() map[string]any {
	return map[string]any{
		"project":           map[string]any{"id": 1, "path_with_namespace": "group/project"},
		"object_attributes": map[string]any{"iid": 7, "action": "update", "state": "opened", "last_commit": map[string]any{"id": "head1"}},
		"labels":            []map[string]any{{"title": "ai-review"}},
		"changes": map[string]any{"labels": map[string]any{"previous": []any{},
			"current": []map[string]any{{"title": "ai-review"}}}},
	}
}

func bodies(ds []fakegitlab.Discussion) []string {
	var out []string
	for _, d := range ds {
		for _, n := range d.Notes {
			out = append(out, n.Body)
		}
	}
	return out
}

func TestLabelToReviewToThreads(t *testing.T) {
	r := newRig(t)
	r.webhook(t, "merge_request", labelEvent())
	if n := r.drain(t); n != 1 {
		t.Fatalf("ran %d tasks", n)
	}
	ds := r.gl.Snapshot(7)
	all := strings.Join(bodies(ds), "\n")
	for _, want := range []string{"Habodek is reviewing", "get_user returns None", "One issue found",
		"<!-- habodek:finding:f1 -->", "<!-- habodek:summary:head1 -->"} {
		if !strings.Contains(all, want) {
			t.Fatalf("missing %q in %s", want, all)
		}
	}
	var inline *fakegitlab.Note
	for _, d := range ds {
		if d.Notes[0].Position != nil {
			inline = d.Notes[0]
		}
	}
	if inline == nil || inline.Position["new_line"].(float64) != 28 || inline.Position["base_sha"] != "base1" ||
		inline.Position["start_sha"] != "start1" || inline.Position["old_line"] != nil {
		t.Fatalf("position = %+v", inline)
	}
	stored, _ := r.store.Findings(context.Background(), 1, 7, true)
	if len(stored) != 1 || stored[0].HeadSHA != "head1" ||
		strings.Join(stored[0].ChainPaths, ",") != "store/users.py,cache/users.py" {
		t.Fatalf("stored = %+v", stored)
	}
	// A retry (same head) posts nothing new.
	before := len(bodies(r.gl.Snapshot(7)))
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	if after := len(bodies(r.gl.Snapshot(7))); after != before {
		t.Fatalf("retry posted %d new notes", after-before)
	}
}

func TestRejectedLineFallsBackToFileLevel(t *testing.T) {
	r := newRig(t)
	r.gl.RejectLines["store/users.py:28"] = true
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	var found bool
	for _, d := range r.gl.Snapshot(7) {
		n := d.Notes[0]
		if n.Position != nil && n.Position["position_type"] == "file" {
			found = strings.Contains(n.Body, "store/users.py:28") && strings.Contains(n.Body, "<!-- habodek:finding:f1 -->")
		}
	}
	if !found {
		t.Fatal("no file-level fallback thread")
	}
}

func TestHeadMovedPostsNothingStale(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	task, _ := r.store.Enqueue(ctx, Task{Kind: KindReview, ProjectID: 1, ProjectPath: "group/project", MRIID: 7,
		ThreadKey: reviewKey(1, 7), Priority: 2})
	claimed, _ := r.store.Claim(ctx, "w", time.Minute)
	// The developer pushes while the brain runs.
	r.worker.Brain = brainFunc(func(ctx context.Context, job brain.Job) (*brain.Result, error) {
		r.gl.MRs[7].HeadSHA = "head2"
		return (&cannedBrain{}).Run(ctx, job)
	})
	r.worker.RunTask(ctx, claimed)
	for _, b := range bodies(r.gl.Snapshot(7)) {
		if strings.Contains(b, "habodek:finding") {
			t.Fatal("posted a finding for a stale head")
		}
	}
	next, _ := r.store.Claim(ctx, "w", time.Minute)
	if next == nil || next.Kind != KindReview || next.ID == task.ID {
		t.Fatalf("no fresh review queued: %+v", next)
	}
}

type brainFunc func(context.Context, brain.Job) (*brain.Result, error)

func (f brainFunc) Run(ctx context.Context, job brain.Job) (*brain.Result, error) { return f(ctx, job) }

func discussionOf(t *testing.T, r *rig) string {
	stored, _ := r.store.Findings(context.Background(), 1, 7, false)
	if len(stored) != 1 {
		t.Fatalf("findings = %d", len(stored))
	}
	return stored[0].DiscussionID
}

func notePayload(userID int64, discussion string) map[string]any {
	return map[string]any{
		"project":           map[string]any{"id": 1, "path_with_namespace": "group/project"},
		"user":              map[string]any{"id": userID},
		"object_attributes": map[string]any{"id": 5, "noteable_type": "MergeRequest", "discussion_id": discussion},
		"merge_request":     map[string]any{"iid": 7},
	}
}

func TestReplyIsDebouncedAnsweredAndNeverResolved(t *testing.T) {
	r := newRig(t)
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	disc := discussionOf(t, r)
	r.gl.HumanReply(7, disc, 5, "ownership is enforced by middleware")
	r.webhook(t, "note", notePayload(5, disc))
	r.webhook(t, "note", notePayload(botID, disc)) // the bot's own note: ignored
	if task, _ := r.store.Claim(context.Background(), "w", time.Minute); task != nil {
		t.Fatal("reply claimed before the debounce")
	}
	if n := r.drain(t); n != 1 {
		t.Fatalf("ran %d tasks for one reply", n)
	}
	thread := r.brain.jobs[len(r.brain.jobs)-1]
	if thread.Kind != "thread" || thread.Event != "reply" || thread.Priority != "reply" ||
		thread.Revisions.BaseSHA != "head1" || thread.Finding.ID != "f1" {
		t.Fatalf("thread job = %+v", thread)
	}
	if len(thread.Notes) != 2 || !thread.Notes[0].IsBot || thread.Notes[1].IsBot ||
		strings.Contains(thread.Notes[0].Body, "habodek:finding") {
		t.Fatalf("notes = %+v", thread.Notes)
	}
	var reply string
	for _, d := range r.gl.Snapshot(7) {
		if d.ID == disc {
			reply = d.Notes[len(d.Notes)-1].Body
		}
	}
	if !strings.HasPrefix(reply, "You're right, withdrawn.") || !strings.Contains(reply, "habodek:reply:") {
		t.Fatalf("reply = %q", reply)
	}
	stored, _ := r.store.FindingByDiscussion(context.Background(), 1, 7, disc)
	if stored.Status != "withdrawn" || stored.Lesson != "middleware covers v1" {
		t.Fatalf("stored = %+v", stored)
	}
	if r.gl.Resolved != 0 {
		t.Fatal("the bot resolved a thread")
	}
}

func TestPushRechecksOnlyFindingsWhoseChainItTouches(t *testing.T) {
	for _, tc := range []struct {
		changed []string
		recheck bool
	}{{[]string{"cache/users.py"}, true}, {[]string{"docs/readme.md"}, false}} {
		t.Run(fmt.Sprint(tc.changed), func(t *testing.T) {
			r := newRig(t)
			r.webhook(t, "merge_request", labelEvent())
			r.drain(t)
			r.git.changed = tc.changed
			r.gl.MRs[7].HeadSHA = "head2"
			ev := labelEvent()
			ev["object_attributes"] = map[string]any{"iid": 7, "action": "update", "state": "opened",
				"oldrev": "head1", "last_commit": map[string]any{"id": "head2"}}
			delete(ev, "changes")
			r.webhook(t, "merge_request", ev)
			before := len(r.brain.jobs)
			r.drain(t)
			rechecks := 0
			for _, j := range r.brain.jobs[before:] {
				if j.Kind == "thread" && j.Event == "push" && j.Priority == "recheck" {
					rechecks++
				}
			}
			if (rechecks == 1) != tc.recheck || len(r.brain.jobs)-before > 1 {
				t.Fatalf("rechecks = %d, jobs = %d", rechecks, len(r.brain.jobs)-before)
			}
		})
	}
}

func TestWebhookRejectsBadToken(t *testing.T) {
	r := newRig(t)
	req, _ := http.NewRequest(http.MethodPost, r.hook.URL, strings.NewReader(`{}`))
	req.Header.Set("X-Gitlab-Token", "wrong")
	resp, _ := http.DefaultClient.Do(req)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}
