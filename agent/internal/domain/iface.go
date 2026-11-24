package domain

import "context"

// GitLabClient is the GitLab REST API subset the control plane uses.
type GitLabClient interface {
	GetMR(ctx context.Context, projectID, mrIID int64) (*GitLabMR, error)
	GetProject(ctx context.Context, projectID int64) (*GitLabProject, error)
	ListMRFiles(ctx context.Context, projectID, mrIID int64) ([]GitLabMRFile, error)
	GetMRDiscussions(ctx context.Context, projectID, mrIID int64) ([]GitLabDiscussion, error)
	GetDiscussion(ctx context.Context, projectID, mrIID int64, discussionID string) (*GitLabDiscussion, error)
	PostInlineComment(ctx context.Context, req PostInlineCommentRequest) (*PostCommentResponse, error)
	PostThreadComment(ctx context.Context, projectID, mrIID int64, body string) (*PostCommentResponse, error)
	PostReply(ctx context.Context, projectID, mrIID int64, discussionID string, body string) (*PostCommentResponse, error)
	ResolveDiscussion(ctx context.Context, projectID, mrIID int64, discussionID string) error
}
