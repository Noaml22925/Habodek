package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"testing"

	"habodek/internal/core/brain"
)

// seed stores posted findings of one category with the given thread verdicts.
func seed(t *testing.T, s *Store, project int64, category string, verdicts ...string) {
	t.Helper()
	for i, v := range verdicts {
		body, _ := json.Marshal(brain.Finding{ID: fmt.Sprintf("%s-%d", category, i), Category: category + ",other"})
		id := fmt.Sprintf("%s-%d", category, i)
		if err := s.SaveFinding(context.Background(), StoredFinding{ProjectID: project, MRIID: int64(100 + i),
			FindingID: id, DiscussionID: "d-" + id, HeadSHA: "h", FindingJSON: body}); err != nil {
			t.Fatal(err)
		}
		if v != "" {
			if err := s.RecordVerdict(context.Background(), project, int64(100+i), id, v, ""); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestAcceptanceAndBars(t *testing.T) {
	r := newRig(t)
	seed(t, r.store, 1, "consumers", "fixed", "withdrawn", "withdrawn", "withdrawn", "withdrawn", "withdrawn")
	seed(t, r.store, 1, "intent", "fixed", "stands", "fix_incomplete", "fixed", "withdrawn", "withdrawn")
	seed(t, r.store, 1, "security", "withdrawn", "withdrawn", "withdrawn", "", "unclear")
	seed(t, r.store, 1, "failure", "withdrawn", "withdrawn", "withdrawn", "fixed", "fixed", "stands", "withdrawn")
	seed(t, r.store, 2, "consumers", "withdrawn", "withdrawn", "withdrawn", "withdrawn", "withdrawn")
	stats, err := r.store.Acceptance(context.Background(), 1, BarConfig{})
	if err != nil {
		t.Fatal(err)
	}
	by := map[string]Acceptance{}
	for _, a := range stats {
		by[a.Category] = a
	}
	if c := by["consumers"]; c.Posted != 6 || c.Accepted != 1 || c.Rejected != 5 {
		t.Fatalf("consumers %+v", c)
	}
	if s := by["security"]; s.Open != 1 || s.Neutral != 1 || s.Outcomes() != 3 {
		t.Fatalf("security %+v", s)
	}
	bars := map[string]string{}
	for _, b := range Bars(stats, BarConfig{}) {
		bars[b.Category] = b.MinSeverity
	}
	// consumers 1/6 -> critical; failure 3/7 -> major; intent 4/6 fine; security: too few outcomes
	if len(bars) != 2 || bars["consumers"] != "critical" || bars["failure"] != "major" {
		t.Fatalf("bars %v", bars)
	}
	if Bars(stats, BarConfig{Disabled: true}) != nil {
		t.Fatal("bars when disabled")
	}
	// the window: only the most recent posted findings of a category count
	small, _ := r.store.Acceptance(context.Background(), 1, BarConfig{Window: 2})
	for _, a := range small {
		if a.Posted > 2 {
			t.Fatalf("window not applied: %+v", a)
		}
	}
}

func TestReviewJobsCarryTheRaisedBars(t *testing.T) {
	r := newRig(t)
	seed(t, r.store, 1, "consumers", "withdrawn", "withdrawn", "withdrawn", "withdrawn", "fixed", "withdrawn")
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	job := r.brain.jobs[0]
	if job.Policy == nil || len(job.Policy.Bars) != 1 || job.Policy.Bars[0].Category != "consumers" ||
		job.Policy.Bars[0].MinSeverity != "critical" || job.Policy.Bars[0].Outcomes != 6 {
		t.Fatalf("policy %+v", job.Policy)
	}
	data, _ := json.Marshal(job)
	if !json.Valid(data) {
		t.Fatal("job json")
	}
}

func TestNoBarsNoPolicy(t *testing.T) {
	r := newRig(t)
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	if r.brain.jobs[0].Policy != nil {
		t.Fatal("policy without outcomes")
	}
}

func TestMetricsEndpoint(t *testing.T) {
	r := newRig(t)
	seed(t, r.store, 1, "intent", "withdrawn", "withdrawn", "withdrawn", "withdrawn", "withdrawn")
	srv := httptest.NewServer(MetricsHandler(r.store, BarConfig{}))
	defer srv.Close()
	resp, err := srv.Client().Get(srv.URL + "?project=1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var rep Report
	if err := json.NewDecoder(resp.Body).Decode(&rep); err != nil {
		t.Fatal(err)
	}
	if len(rep.Categories) != 1 || rep.Categories[0].Rate != 0 || len(rep.Bars) != 1 {
		t.Fatalf("report %+v", rep)
	}
	bad, _ := srv.Client().Get(srv.URL)
	if bad.StatusCode != 400 {
		t.Fatalf("missing project: %d", bad.StatusCode)
	}
}

func TestRepositoryPostingPolicy(t *testing.T) {
	r := newRig(t)
	r.worker.Config.MinSeverity = "minor"
	r.worker.Config.Repos = map[string]RepoPolicy{"group/project": {MinSeverity: "major", MaxComments: 2}}
	r.webhook(t, "merge_request", labelEvent())
	r.drain(t)
	job := r.brain.jobs[0]
	if job.MaxComments != 2 || job.Policy == nil || job.Policy.MinSeverity != "major" || len(job.Policy.Bars) != 0 {
		t.Fatalf("job policy %+v max %d", job.Policy, job.MaxComments)
	}
	if r.worker.maxComments("other/project") != r.worker.Config.MaxComments ||
		r.worker.minSeverity("other/project") != "minor" {
		t.Fatal("an unlisted project takes the defaults")
	}
}
