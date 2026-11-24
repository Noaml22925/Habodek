package orchestrator

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"habodek/internal/pkg/llmproxy"
)

// Triggers turns GitLab webhooks into tasks (handoff M8):
//   - the trigger label added to an MR (or an MR opened with it) -> a review;
//   - new commits on a labelled MR -> a push task, which re-checks every open finding
//     whose evidence chain the commits touch;
//   - a human note in a bot thread -> a thread task, debounced so "fixed" and the push
//     that goes with it arrive as one event.
type Triggers struct {
	Store         *Store
	WebhookSecret string
	BotUserID     int64
	TriggerLabel  string
	Debounce      time.Duration

	seenMu sync.Mutex
	seen   map[string]time.Time // webhook delivery ids, to drop GitLab's retries
}

type mrEvent struct {
	Project struct {
		ID   int64  `json:"id"`
		Path string `json:"path_with_namespace"`
	} `json:"project"`
	Attrs struct {
		IID        int64  `json:"iid"`
		Action     string `json:"action"`
		State      string `json:"state"`
		OldRev     string `json:"oldrev"`
		LastCommit struct {
			ID string `json:"id"`
		} `json:"last_commit"`
	} `json:"object_attributes"`
	Labels []struct {
		Title string `json:"title"`
	} `json:"labels"`
	Changes struct {
		Labels *struct {
			Previous []struct {
				Title string `json:"title"`
			} `json:"previous"`
			Current []struct {
				Title string `json:"title"`
			} `json:"current"`
		} `json:"labels"`
	} `json:"changes"`
}

type noteEvent struct {
	Project struct {
		ID   int64  `json:"id"`
		Path string `json:"path_with_namespace"`
	} `json:"project"`
	User struct {
		ID int64 `json:"id"`
	} `json:"user"`
	Attrs struct {
		ID           int64  `json:"id"`
		NoteableType string `json:"noteable_type"`
		DiscussionID string `json:"discussion_id"`
	} `json:"object_attributes"`
	MergeRequest *struct {
		IID int64 `json:"iid"`
	} `json:"merge_request"`
}

func reviewKey(projectID, mrIID int64) string { return fmt.Sprintf("review:%d:%d", projectID, mrIID) }
func threadKey(projectID, mrIID int64, discussionID string) string {
	return fmt.Sprintf("thread:%d:%d:%s", projectID, mrIID, discussionID)
}

// ServeHTTP handles POST /webhook/gitlab.
func (t *Triggers) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if t.WebhookSecret != "" && subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Gitlab-Token")),
		[]byte(t.WebhookSecret)) != 1 {
		http.Error(w, "bad token", http.StatusUnauthorized)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if err != nil {
		http.Error(w, "unreadable body", http.StatusBadRequest)
		return
	}
	if id := r.Header.Get("X-Gitlab-Event-UUID"); id != "" && t.duplicate(id) {
		w.WriteHeader(http.StatusOK)
		return
	}
	var kind struct {
		ObjectKind string `json:"object_kind"`
	}
	_ = json.Unmarshal(body, &kind)
	var tasks []string
	switch kind.ObjectKind {
	case "merge_request":
		tasks, err = t.mergeRequest(r.Context(), body)
	case "note":
		tasks, err = t.note(r.Context(), body)
	}
	if err != nil {
		slog.Error("webhook", "kind", kind.ObjectKind, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError) // GitLab retries
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"queued": tasks})
}

func (t *Triggers) duplicate(id string) bool {
	t.seenMu.Lock()
	defer t.seenMu.Unlock()
	if t.seen == nil {
		t.seen = map[string]time.Time{}
	}
	now := time.Now()
	for k, at := range t.seen {
		if now.Sub(at) > time.Hour {
			delete(t.seen, k)
		}
	}
	if _, ok := t.seen[id]; ok {
		return true
	}
	t.seen[id] = now
	return false
}

func hasLabel(labels []struct {
	Title string `json:"title"`
}, want string) bool {
	for _, l := range labels {
		if l.Title == want {
			return true
		}
	}
	return false
}

func (t *Triggers) mergeRequest(ctx context.Context, body []byte) ([]string, error) {
	var ev mrEvent
	if err := json.Unmarshal(body, &ev); err != nil {
		return nil, err
	}
	if ev.Attrs.State != "" && ev.Attrs.State != "opened" {
		return nil, nil
	}
	if !hasLabel(ev.Labels, t.TriggerLabel) {
		return nil, nil
	}
	pid, iid, path := ev.Project.ID, ev.Attrs.IID, ev.Project.Path
	labelAdded := ev.Changes.Labels != nil && !hasLabel(ev.Changes.Labels.Previous, t.TriggerLabel) &&
		hasLabel(ev.Changes.Labels.Current, t.TriggerLabel)
	var queued []string
	if ev.Attrs.Action == "open" || ev.Attrs.Action == "reopen" || labelAdded {
		task, err := t.Store.Enqueue(ctx, Task{Kind: KindReview, ProjectID: pid, ProjectPath: path,
			MRIID: iid, ThreadKey: reviewKey(pid, iid), Priority: llmproxy.PriorityReview,
			Payload: map[string]any{"head_sha": ev.Attrs.LastCommit.ID}})
		if err != nil {
			return nil, err
		}
		queued = append(queued, task.ID)
	}
	if ev.Attrs.Action == "update" && ev.Attrs.OldRev != "" {
		task, err := t.Store.Enqueue(ctx, Task{Kind: KindPush, ProjectID: pid, ProjectPath: path,
			MRIID: iid, ThreadKey: fmt.Sprintf("push:%d:%d", pid, iid), Priority: llmproxy.PriorityRecheck,
			Payload: map[string]any{"head_sha": ev.Attrs.LastCommit.ID, "old_sha": ev.Attrs.OldRev},
			// Let a reply that comes with the push land first; the thread tasks merge.
			NotBefore: time.Now().Add(t.Debounce)})
		if err != nil {
			return nil, err
		}
		queued = append(queued, task.ID)
	}
	return queued, nil
}

func (t *Triggers) note(ctx context.Context, body []byte) ([]string, error) {
	var ev noteEvent
	if err := json.Unmarshal(body, &ev); err != nil {
		return nil, err
	}
	if ev.Attrs.NoteableType != "MergeRequest" || ev.MergeRequest == nil || ev.User.ID == t.BotUserID ||
		ev.Attrs.DiscussionID == "" {
		return nil, nil
	}
	pid, iid := ev.Project.ID, ev.MergeRequest.IID
	finding, err := t.Store.FindingByDiscussion(ctx, pid, iid, ev.Attrs.DiscussionID)
	if err != nil || finding == nil {
		return nil, err // not a bot thread: nothing to do
	}
	task, err := t.Store.Enqueue(ctx, Task{Kind: KindThread, ProjectID: pid, ProjectPath: ev.Project.Path,
		MRIID: iid, ThreadKey: threadKey(pid, iid, ev.Attrs.DiscussionID), Priority: llmproxy.PriorityReply,
		Event: "reply", NotBefore: time.Now().Add(t.Debounce),
		Payload: map[string]any{"finding_id": finding.FindingID, "discussion_id": ev.Attrs.DiscussionID}})
	if err != nil {
		return nil, err
	}
	return []string{task.ID}, nil
}
