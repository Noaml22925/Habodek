package orchestrator

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"habodek/internal/pkg/memory"
)

type recordingApplier struct {
	mu   sync.Mutex
	jobs []string
}

func (a *recordingApplier) ApplyMemory(_ context.Context, _, jobID string) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.jobs = append(a.jobs, jobID)
	return "c0ffee", nil
}

// memoryDir is a memory repository with one commit and the given facts files.
func memoryDir(t *testing.T, facts map[string]map[string]any, cards ...string) string {
	t.Helper()
	dir := t.TempDir()
	write := func(rel string, data []byte) {
		p := filepath.Join(dir, rel)
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("README.md", []byte("memory\n"))
	for project, f := range facts {
		data, _ := json.Marshal(f)
		write("projects/"+project+"/facts.json", data)
	}
	for _, project := range cards {
		write("projects/"+project+"/card.md", []byte("# card\n"))
	}
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"add", "-A"},
		{"-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "m"}} {
		if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
			t.Fatalf("git %v: %s", args, out)
		}
	}
	return dir
}

func TestFirstReviewMapsTheProjectInTheBackground(t *testing.T) {
	r := newRig(t)
	applier := &recordingApplier{}
	r.worker.Memory = memory.New(memoryDir(t, nil))
	r.worker.Applier = applier
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	var review, mapJob = r.brain.jobs[0], r.brain.jobs[1]
	if review.Kind != "review" || review.MemoryPath == nil || review.MemoryCommit == nil ||
		*review.MemoryPath != r.worker.Memory.Dir || len(*review.MemoryCommit) != 40 {
		t.Fatalf("review job memory: %+v", review)
	}
	if mapJob.Kind != "map" || mapJob.Priority != "background" || mapJob.MR.ProjectID != 1 ||
		mapJob.MR.TargetBranch != "main" || r.ws.branches[0] != "main" {
		t.Fatalf("map job: %+v", mapJob)
	}
	if len(applier.jobs) != 1 { // the map result's card; the review had no updates
		t.Fatalf("applied %v", applier.jobs)
	}
	task, _ := r.store.Get(context.Background(), applier.jobs[0])
	if task == nil || task.Kind != KindMap || task.Priority != 3 {
		t.Fatalf("map task %+v", task)
	}
}

func TestAMappedProjectIsNotRemappedOnEveryReview(t *testing.T) {
	r := newRig(t)
	r.worker.Memory = memory.New(memoryDir(t, nil, "group/project"))
	r.worker.Applier = &recordingApplier{}
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	for _, j := range r.brain.jobs {
		if j.Kind == "map" {
			t.Fatal("mapped again although a card exists")
		}
	}
}

func TestLibraryReviewChecksOutItsConsumers(t *testing.T) {
	r := newRig(t)
	r.worker.Memory = memory.New(memoryDir(t, map[string]map[string]any{
		"group/project": {"project": "group/project", "package": "platform-common", "gitlab_id": 1},
		"team/orders-service": {"project": "team/orders-service", "gitlab_id": 12, "default_branch": "main",
			"pins": map[string]string{"platform_common": "2.3.0"}, "mapped_at": "2026-11-20T00:00:00Z"},
		"team/billing": {"project": "team/billing", "gitlab_id": 13, "default_branch": "develop",
			"pins": map[string]string{"requests": "2.0"}},
		"scenarios/local": {"project": "scenarios/local", "pins": map[string]string{"platform-common": "2.3.0"}},
	}, "group/project"))
	r.worker.Applier = &recordingApplier{}
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	job := r.brain.jobs[0]
	if len(r.ws.consumers) != 1 || r.ws.consumers[0].ProjectID != 12 || r.ws.consumers[0].Branch != "main" {
		t.Fatalf("consumers %+v", r.ws.consumers)
	}
	if job.Workspace.OtherRepos["orders-service"] != "/w/team/orders-service" {
		t.Fatalf("other repos %+v", job.Workspace.OtherRepos)
	}
}

func TestAVerifiedWithdrawalIsAppliedToMemory(t *testing.T) {
	r := newRig(t)
	applier := &recordingApplier{}
	r.worker.Memory = memory.New(memoryDir(t, nil, "group/project"))
	r.worker.Applier = applier
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	disc := discussionOf(t, r)
	r.gl.HumanReply(7, disc, 5, "ownership is enforced by middleware")
	r.webhook(t, "note", notePayload(5, disc))
	r.drain(t)
	if len(applier.jobs) != 1 {
		t.Fatalf("applied %v", applier.jobs)
	}
	task, _ := r.store.Get(context.Background(), applier.jobs[0])
	if task.Kind != KindThread {
		t.Fatalf("applied for %+v", task)
	}
}

type cards map[string]bool

func (c cards) HasCard(p string) bool { return c[p] }

func TestPushToTheDefaultBranchRemapsOnlyStructuralChanges(t *testing.T) {
	r := newRig(t)
	r.triggers.Memory = cards{"group/project": true}
	r.triggers.MapDebounce = 30 * time.Millisecond
	push := func(ref string, commits []map[string]any) {
		r.webhook(t, "push", map[string]any{"ref": ref, "commits": commits,
			"project": map[string]any{"id": 1, "path_with_namespace": "group/project", "default_branch": "main"}})
	}
	queued := func() int {
		var n int
		_ = r.store.db.QueryRow(`SELECT COUNT(*) FROM hbk_tasks WHERE kind = 'map' AND status = 'queued'`).Scan(&n)
		return n
	}
	push("refs/heads/main", []map[string]any{{"modified": []string{"orders/api.py"}}})
	push("refs/heads/feature", []map[string]any{{"added": []string{"x.py"}}})
	if queued() != 0 {
		t.Fatal("a plain edit or a feature branch triggered a map")
	}
	push("refs/heads/main", []map[string]any{{"added": []string{"orders/new.py"}}})
	push("refs/heads/main", []map[string]any{{"modified": []string{"requirements.txt"}}})
	if queued() != 1 {
		t.Fatalf("queued maps = %d, want 1 (the second supersedes the first)", queued())
	}
	r.triggers.Memory = cards{}
	push("refs/heads/main", []map[string]any{{"added": []string{"y.py"}}})
	if queued() != 1 {
		t.Fatal("an unmapped project was mapped from a push")
	}
}

func TestMemoryStoreReadsFactsAndHead(t *testing.T) {
	dir := memoryDir(t, map[string]map[string]any{
		"a/lib": {"project": "a/lib", "package": "Lib_X"},
		"a/app": {"project": "a/app", "pins": map[string]string{"lib-x": "1"}, "gitlab_id": 4}}, "a/lib")
	s := memory.New(dir)
	if !s.HasCard("a/lib") || s.HasCard("a/app") || len(s.Head(context.Background())) != 40 {
		t.Fatal("card/head")
	}
	if c := s.Consumers("a/lib"); len(c) != 1 || c[0].Project != "a/app" {
		t.Fatalf("consumers %+v", c)
	}
	if memory.New(t.TempDir()).Head(context.Background()) != "" {
		t.Fatal("an empty directory has no head")
	}
}
