// Package fakegitlab is an in-memory GitLab REST API (the subset Habodek uses), for
// tests of the control plane: merge requests with diff_refs, discussions, notes,
// positions. Lines listed in RejectLines are refused like GitLab refuses a line that
// is not in the diff, to exercise the posting fallback.
package fakegitlab

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"sync"
	"time"
)

type Note struct {
	ID         int64          `json:"id"`
	Body       string         `json:"body"`
	Author     Author         `json:"author"`
	Resolvable bool           `json:"resolvable"`
	Resolved   bool           `json:"resolved"`
	Position   map[string]any `json:"position,omitempty"`
	CreatedAt  time.Time      `json:"created_at"`
}

type Author struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
}

type Discussion struct {
	ID    string  `json:"id"`
	Notes []*Note `json:"notes"`
}

type MR struct {
	IID          int64
	Title        string
	Description  string
	SourceBranch string
	TargetBranch string
	HeadSHA      string
	BaseSHA      string
	StartSHA     string
	Labels       []string
	Draft        bool
	Author       string
}

// GitLab is the fake server.
type GitLab struct {
	mu          sync.Mutex
	Server      *httptest.Server
	MRs         map[int64]*MR // by IID (one project)
	Discussions map[int64][]*Discussion
	BotID       int64
	RejectLines map[string]bool // "path:line" refused for line positions
	Resolved    int             // resolve calls (must stay 0)
	nextID      int64
}

func New(botID int64) *GitLab {
	g := &GitLab{MRs: map[int64]*MR{}, Discussions: map[int64][]*Discussion{}, BotID: botID,
		RejectLines: map[string]bool{}, nextID: 1000}
	g.Server = httptest.NewServer(http.HandlerFunc(g.handle))
	return g
}

func (g *GitLab) Close() { g.Server.Close() }

// URL is the base URL to give gitlab.NewClient.
func (g *GitLab) URL() string { return g.Server.URL }

var (
	reMR          = regexp.MustCompile(`^/api/v4/projects/\d+/merge_requests/(\d+)$`)
	reDiscussions = regexp.MustCompile(`^/api/v4/projects/\d+/merge_requests/(\d+)/discussions$`)
	reDiscussion  = regexp.MustCompile(`^/api/v4/projects/\d+/merge_requests/(\d+)/discussions/([^/]+)$`)
	reReply       = regexp.MustCompile(`^/api/v4/projects/\d+/merge_requests/(\d+)/discussions/([^/]+)/notes$`)
	reNotes       = regexp.MustCompile(`^/api/v4/projects/\d+/merge_requests/(\d+)/notes$`)
)

func (g *GitLab) id() int64 { g.nextID++; return g.nextID }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (g *GitLab) handle(w http.ResponseWriter, r *http.Request) {
	g.mu.Lock()
	defer g.mu.Unlock()
	path := r.URL.Path
	body, _ := io.ReadAll(r.Body)
	var req map[string]any
	_ = json.Unmarshal(body, &req)
	if m := reMR.FindStringSubmatch(path); m != nil && r.Method == http.MethodGet {
		iid, _ := strconv.ParseInt(m[1], 10, 64)
		mr := g.MRs[iid]
		if mr == nil {
			writeJSON(w, 404, map[string]string{"message": "404 Not found"})
			return
		}
		writeJSON(w, 200, map[string]any{"iid": mr.IID, "title": mr.Title, "description": mr.Description,
			"source_branch": mr.SourceBranch, "target_branch": mr.TargetBranch, "sha": mr.HeadSHA,
			"web_url": fmt.Sprintf("%s/group/project/-/merge_requests/%d", g.Server.URL, mr.IID),
			"author":  map[string]string{"username": mr.Author}, "labels": mr.Labels, "draft": mr.Draft,
			"diff_refs": map[string]string{"base_sha": mr.BaseSHA, "start_sha": mr.StartSHA,
				"head_sha": mr.HeadSHA}})
		return
	}
	if m := reDiscussions.FindStringSubmatch(path); m != nil {
		iid, _ := strconv.ParseInt(m[1], 10, 64)
		if r.Method == http.MethodGet {
			if r.URL.Query().Get("page") != "" && r.URL.Query().Get("page") != "1" {
				writeJSON(w, 200, []any{})
				return
			}
			writeJSON(w, 200, g.Discussions[iid])
			return
		}
		position, _ := req["position"].(map[string]any)
		if position != nil && position["position_type"] == "text" {
			line := position["new_line"]
			if line == nil {
				line = position["old_line"]
			}
			key := fmt.Sprintf("%v:%v", position["new_path"], line)
			if g.RejectLines[key] {
				writeJSON(w, 400, map[string]any{"message": map[string][]string{
					"base": {"Note {:line_code=>[\"can't be blank\", \"must be a valid line code\"]}"}}})
				return
			}
		}
		d := &Discussion{ID: fmt.Sprintf("disc-%d", g.id()), Notes: []*Note{{ID: g.id(),
			Body: fmt.Sprint(req["body"]), Author: Author{ID: g.BotID, Username: "habodek"},
			Resolvable: true, Position: position, CreatedAt: time.Now()}}}
		g.Discussions[iid] = append(g.Discussions[iid], d)
		writeJSON(w, 201, d)
		return
	}
	if m := reReply.FindStringSubmatch(path); m != nil && r.Method == http.MethodPost {
		iid, _ := strconv.ParseInt(m[1], 10, 64)
		d := g.find(iid, m[2])
		if d == nil {
			writeJSON(w, 404, map[string]string{"message": "404 Discussion Not Found"})
			return
		}
		author := Author{ID: g.BotID, Username: "habodek"}
		note := &Note{ID: g.id(), Body: fmt.Sprint(req["body"]), Author: author, CreatedAt: time.Now()}
		d.Notes = append(d.Notes, note)
		writeJSON(w, 201, note)
		return
	}
	if m := reDiscussion.FindStringSubmatch(path); m != nil {
		iid, _ := strconv.ParseInt(m[1], 10, 64)
		d := g.find(iid, m[2])
		if d == nil {
			writeJSON(w, 404, map[string]string{"message": "404 Discussion Not Found"})
			return
		}
		if r.Method == http.MethodPut {
			g.Resolved++
		}
		writeJSON(w, 200, d)
		return
	}
	if m := reNotes.FindStringSubmatch(path); m != nil && r.Method == http.MethodPost {
		iid, _ := strconv.ParseInt(m[1], 10, 64)
		note := &Note{ID: g.id(), Body: fmt.Sprint(req["body"]), Author: Author{ID: g.BotID, Username: "habodek"},
			CreatedAt: time.Now()}
		g.Discussions[iid] = append(g.Discussions[iid], &Discussion{ID: fmt.Sprintf("disc-%d", g.id()),
			Notes: []*Note{note}})
		writeJSON(w, 201, note)
		return
	}
	writeJSON(w, 404, map[string]string{"message": "no route " + r.Method + " " + path})
}

func (g *GitLab) find(iid int64, id string) *Discussion {
	for _, d := range g.Discussions[iid] {
		if d.ID == id {
			return d
		}
	}
	return nil
}

// HumanReply adds a developer's note to a discussion (what a reply webhook reports).
func (g *GitLab) HumanReply(iid int64, discussionID string, userID int64, body string) int64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	d := g.find(iid, discussionID)
	note := &Note{ID: g.id(), Body: body, Author: Author{ID: userID, Username: "dev"}, CreatedAt: time.Now()}
	d.Notes = append(d.Notes, note)
	return note.ID
}

// Snapshot returns a copy of an MR's discussions.
func (g *GitLab) Snapshot(iid int64) []Discussion {
	g.mu.Lock()
	defer g.mu.Unlock()
	var out []Discussion
	for _, d := range g.Discussions[iid] {
		copyD := Discussion{ID: d.ID}
		for _, n := range d.Notes {
			copyD.Notes = append(copyD.Notes, &Note{ID: n.ID, Body: n.Body, Author: n.Author,
				Position: n.Position})
		}
		out = append(out, copyD)
	}
	return out
}
