// Package domain holds the GitLab and git types shared by the control plane.
package domain

import "time"

type DiffFile struct {
	Path         string `json:"path"`
	OldPath      string `json:"oldPath"`
	Status       string `json:"status"` // "A" | "M" | "D" | "R"
	LinesAdded   int    `json:"linesAdded"`
	LinesRemoved int    `json:"linesRemoved"`
	AddedLines   []int  `json:"addedLines"`
}

type DiffRefs struct {
	BaseSHA  string
	StartSHA string
	HeadSHA  string
}

type GitLabDiscussion struct {
	ID    string
	Notes []GitLabNote
}

type GitLabMR struct {
	IID            int64
	Title          string
	Description    string
	SourceBranch   string
	TargetBranch   string
	HeadSHA        string
	WebURL         string
	AuthorUsername string
	Labels         []string
	Draft          bool
	// DiffRefs is GitLab's own view of the diff; a position must use these SHAs, and
	// posting checks they still match the SHAs that were reviewed.
	DiffRefs DiffRefs
}

type GitLabMRFile struct {
	OldPath     string
	NewPath     string
	NewFile     bool
	DeletedFile bool
	RenamedFile bool
}

type GitLabNote struct {
	ID         int64
	AuthorID   int64
	AuthorName string
	Body       string
	Resolvable bool
	Resolved   bool
	Position   *GitLabNotePosition
	CreatedAt  time.Time
}

type GitLabNotePosition struct {
	FilePath string
	NewLine  int
	OldLine  int
}

type GitLabProject struct {
	ID            int64
	PathWithNS    string
	DefaultBranch string
}

type PostCommentResponse struct {
	NoteID       int64
	DiscussionID string
}

type PostInlineCommentRequest struct {
	ProjectID int64
	MrIID     int64
	Body      string
	FilePath  string
	NewLine   int
	// OldLine/OldPath: a removed line sets only old_line; a context line sets both
	// (GitLab rejects a context line positioned by new_line alone).
	OldLine int
	OldPath string
	// FileLevel posts on the file rather than a line (the fallback when GitLab
	// rejects a line position).
	FileLevel bool
	BaseSHA   string
	HeadSHA   string
	StartSHA  string
}
