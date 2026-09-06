package domain

// ─── Enums ──────────────────────────────────────────────────────────────────────

type ReviewJobStatus string

const (
	ReviewJobStatusPending     ReviewJobStatus = "PENDING"
	ReviewJobStatusReviewing   ReviewJobStatus = "REVIEWING"
	ReviewJobStatusPosting     ReviewJobStatus = "POSTING"
	ReviewJobStatusCompleted   ReviewJobStatus = "COMPLETED"
	ReviewJobStatusFailed      ReviewJobStatus = "FAILED"
	ReviewJobStatusParseFailed ReviewJobStatus = "PARSE_FAILED"
	ReviewJobStatusSkippedSize ReviewJobStatus = "SKIPPED_SIZE"
)

type ReplyJobStatus string

const (
	ReplyJobStatusPending    ReplyJobStatus = "PENDING"
	ReplyJobStatusProcessing ReplyJobStatus = "PROCESSING"
	ReplyJobStatusCompleted  ReplyJobStatus = "COMPLETED"
	ReplyJobStatusFailed     ReplyJobStatus = "FAILED"
)

type FeedbackSignal string

const (
	FeedbackSignalAccepted FeedbackSignal = "ACCEPTED"
	FeedbackSignalRejected FeedbackSignal = "REJECTED"
	FeedbackSignalNeutral  FeedbackSignal = "NEUTRAL"
)

type TriggerSource string

const (
	TriggerSourceWebhook    TriggerSource = "webhook"
	TriggerSourceCLI        TriggerSource = "cli"
	TriggerSourceQueueRetry TriggerSource = "queue_retry"
)

type CommentCategory string

const (
	CategorySecurity    CommentCategory = "security"
	CategoryBug         CommentCategory = "bug"
	CategoryLogic       CommentCategory = "logic"
	CategoryPerformance CommentCategory = "performance"
	CategoryNaming      CommentCategory = "naming"
	CategoryStyle       CommentCategory = "style"
)

type CommentSeverity string

const (
	SeverityCritical CommentSeverity = "critical"
	SeverityHigh     CommentSeverity = "high"
	SeverityMedium   CommentSeverity = "medium"
	SeverityLow      CommentSeverity = "low"
)

type ReplyIntent string

const (
	IntentAgree       ReplyIntent = "agree"
	IntentReject      ReplyIntent = "reject"
	IntentQuestion    ReplyIntent = "question"
	IntentDiscuss     ReplyIntent = "discuss"
	IntentAcknowledge ReplyIntent = "acknowledge"
)

type ThreadState string

const (
	ThreadStateOpen                ThreadState = "open"
	ThreadStateAcknowledged        ThreadState = "acknowledged"
	ThreadStatePendingVerification ThreadState = "pending_verification"
	ThreadStateResolved            ThreadState = "resolved"
	ThreadStateDismissed           ThreadState = "dismissed"
	ThreadStateSuperseded          ThreadState = "superseded"
	// ThreadStateVerifiedFixed means the reviewer re-read the code and confirmed
	// the finding is addressed. The GitLab thread is deliberately left open --
	// resolving it is the developer's call, not the bot's.
	ThreadStateVerifiedFixed ThreadState = "verified_fixed"
)

type RiskTier string

const (
	RiskHigh   RiskTier = "HIGH"
	RiskMedium RiskTier = "MEDIUM"
	RiskLow    RiskTier = "LOW"
)

type QueueJobType string

const (
	QueueJobTypeReview QueueJobType = "review"
	QueueJobTypeReply  QueueJobType = "reply"
)

const (
	DefaultPromptVersion    = "review-prompt-v1"
	DefaultPolicyVersion    = "review-policy-v1"
	DefaultModelPlanVersion = "review-model-plan-v1"
)

// ReplyVerdict is the reviewer's own conclusion about one of its findings,
// decided by the model rather than inferred from keywords in the developer's
// reply. It is what closes an argument: the reviewer either stands by the
// finding, withdraws it, or confirms the code now fixes it.
type ReplyVerdict string

const (
	// VerdictStands — the finding still applies; the developer's argument or
	// fix does not address it.
	VerdictStands ReplyVerdict = "stands"
	// VerdictWithdrawn — the developer is right and the finding was wrong.
	VerdictWithdrawn ReplyVerdict = "withdrawn"
	// VerdictFixed — the current code genuinely resolves the finding.
	VerdictFixed ReplyVerdict = "fixed"
	// VerdictFixIncomplete — the code changed and helps, but part of the
	// original concern survives.
	VerdictFixIncomplete ReplyVerdict = "fix_incomplete"
	// VerdictUnclear — not enough information to decide either way. Said out
	// loud rather than defaulted to agreement.
	VerdictUnclear ReplyVerdict = "unclear"
)

// ValidReplyVerdict reports whether v is a verdict the pipeline understands.
func ValidReplyVerdict(v ReplyVerdict) bool {
	switch v {
	case VerdictStands, VerdictWithdrawn, VerdictFixed, VerdictFixIncomplete, VerdictUnclear:
		return true
	default:
		return false
	}
}
