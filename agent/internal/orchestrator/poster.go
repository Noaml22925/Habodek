package orchestrator

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"habodek/internal/core/brain"
	"habodek/internal/domain"
)

// Markers are hidden in every note the bot writes, so a retried job never posts twice.
func progressMarker(headSHA string) string {
	return fmt.Sprintf("<!-- habodek:progress:%s -->", headSHA)
}
func summaryMarker(headSHA string) string { return fmt.Sprintf("<!-- habodek:summary:%s -->", headSHA) }
func replyMarker(taskID string) string    { return fmt.Sprintf("<!-- habodek:reply:%s -->", taskID) }

// ErrHeadMoved means the MR changed after the review started: its result is stale.
var ErrHeadMoved = fmt.Errorf("the merge request head moved since the review started")

// PostedThread is where a finding was posted.
type PostedThread struct {
	FindingID    string
	DiscussionID string
	NoteID       int64
	FileLevel    bool
}

// Poster writes the brain's results to GitLab, following PR-Agent's GitLab lessons
// (survey pragent-hermes.md): position rules, a diff_refs check before posting, a
// file-level fallback when a line is rejected, idempotent writes, never resolving.
type Poster struct {
	GitLab domain.GitLabClient
}

// existingMarkers returns every bot marker already present on the MR, mapped to the
// discussion and note that carry it.
func (p *Poster) existingMarkers(ctx context.Context, projectID, mrIID int64) (map[string]PostedThread, error) {
	discussions, err := p.GitLab.GetMRDiscussions(ctx, projectID, mrIID)
	if err != nil {
		return nil, err
	}
	out := map[string]PostedThread{}
	for _, d := range discussions {
		for _, n := range d.Notes {
			for _, marker := range markersIn(n.Body) {
				out[marker] = PostedThread{DiscussionID: d.ID, NoteID: n.ID}
			}
		}
	}
	return out, nil
}

func markersIn(body string) []string {
	var out []string
	for {
		i := strings.Index(body, "<!-- habodek:")
		if i < 0 {
			return out
		}
		j := strings.Index(body[i:], "-->")
		if j < 0 {
			return out
		}
		out = append(out, body[i:i+j+3])
		body = body[i+j+3:]
	}
}

// Progress posts the "review in progress" note once per head SHA.
func (p *Poster) Progress(ctx context.Context, projectID, mrIID int64, headSHA, text string) error {
	marker := progressMarker(headSHA)
	existing, err := p.existingMarkers(ctx, projectID, mrIID)
	if err != nil {
		return err
	}
	if _, ok := existing[marker]; ok {
		return nil
	}
	_, err = p.GitLab.PostThreadComment(ctx, projectID, mrIID, text+"\n\n"+marker)
	return err
}

// Review posts a review result: one thread per comment, then the summary. It first
// checks that GitLab's diff_refs still match the reviewed SHAs.
func (p *Poster) Review(ctx context.Context, projectID, mrIID int64, reviewed brain.Revisions,
	result *brain.Result) ([]PostedThread, error) {
	mr, err := p.GitLab.GetMR(ctx, projectID, mrIID)
	if err != nil {
		return nil, err
	}
	if mr.HeadSHA != reviewed.HeadSHA || (mr.DiffRefs.HeadSHA != "" && mr.DiffRefs.HeadSHA != reviewed.HeadSHA) {
		return nil, ErrHeadMoved
	}
	refs := mr.DiffRefs
	if refs.BaseSHA == "" {
		refs = domain.DiffRefs{BaseSHA: reviewed.BaseSHA, StartSHA: reviewed.StartSHA, HeadSHA: reviewed.HeadSHA}
	}
	if refs.StartSHA == "" {
		refs.StartSHA = refs.BaseSHA
	}
	existing, err := p.existingMarkers(ctx, projectID, mrIID)
	if err != nil {
		return nil, err
	}
	var posted []PostedThread
	for _, c := range result.Comments {
		if prior, ok := existing[c.Marker]; ok { // idempotent: a retry finds its own note
			prior.FindingID = c.FindingID
			posted = append(posted, prior)
			continue
		}
		thread, err := p.postComment(ctx, projectID, mrIID, refs, c)
		if err != nil {
			return posted, fmt.Errorf("post %s: %w", c.FindingID, err)
		}
		posted = append(posted, thread)
	}
	if strings.TrimSpace(result.Summary) != "" {
		marker := summaryMarker(reviewed.HeadSHA)
		if _, ok := existing[marker]; !ok {
			body := "**Habodek review**\n\n" + result.Summary + "\n\n" + marker
			if _, err := p.GitLab.PostThreadComment(ctx, projectID, mrIID, body); err != nil {
				return posted, fmt.Errorf("post summary: %w", err)
			}
		}
	}
	return posted, nil
}

// postComment tries the line position, then a file-level position, then a plain MR
// thread that names the location. A comment is never dropped because GitLab refused
// its line.
func (p *Poster) postComment(ctx context.Context, projectID, mrIID int64, refs domain.DiffRefs,
	c brain.Comment) (PostedThread, error) {
	req := domain.PostInlineCommentRequest{ProjectID: projectID, MrIID: mrIID, Body: c.Body,
		FilePath: c.Path, BaseSHA: refs.BaseSHA, StartSHA: refs.StartSHA, HeadSHA: refs.HeadSHA}
	if c.Side == "old" { // a removed line: old_line only
		req.OldLine = c.Line
	} else { // an added line: new_line only
		req.NewLine = c.Line
	}
	resp, err := p.GitLab.PostInlineComment(ctx, req)
	if err == nil {
		return PostedThread{FindingID: c.FindingID, DiscussionID: resp.DiscussionID, NoteID: resp.NoteID}, nil
	}
	slog.Warn("line position rejected; posting on the file", "finding", c.FindingID, "path", c.Path,
		"line", c.Line, "error", err)
	req.FileLevel, req.NewLine, req.OldLine = true, 0, 0
	req.Body = fmt.Sprintf("_(On `%s:%d`; GitLab did not accept a comment on that line.)_\n\n%s",
		c.Path, c.Line, c.Body)
	if resp, err2 := p.GitLab.PostInlineComment(ctx, req); err2 == nil {
		return PostedThread{FindingID: c.FindingID, DiscussionID: resp.DiscussionID, NoteID: resp.NoteID,
			FileLevel: true}, nil
	}
	resp, err = p.GitLab.PostThreadComment(ctx, projectID, mrIID, req.Body)
	if err != nil {
		return PostedThread{}, err
	}
	return PostedThread{FindingID: c.FindingID, DiscussionID: resp.DiscussionID, NoteID: resp.NoteID,
		FileLevel: true}, nil
}

// Reply posts a thread verdict as a reply in the finding's discussion, once per task.
// It never resolves the thread: that stays the developer's call.
func (p *Poster) Reply(ctx context.Context, projectID, mrIID int64, discussionID, taskID, body string) error {
	marker := replyMarker(taskID)
	discussion, err := p.GitLab.GetDiscussion(ctx, projectID, mrIID, discussionID)
	if err != nil {
		return err
	}
	for _, n := range discussion.Notes {
		if strings.Contains(n.Body, marker) {
			return nil
		}
	}
	_, err = p.GitLab.PostReply(ctx, projectID, mrIID, discussionID, body+"\n\n"+marker)
	return err
}
