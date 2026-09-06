package review

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"habodek/internal/core/agents/replier"
	"habodek/internal/core/prompt"
	"habodek/internal/domain"
)

// verdictCodeContextRadius is how many lines either side of a finding's anchor
// are handed to the re-check pass. Wide enough to contain a function, narrow
// enough that N open threads do not blow the context window.
const verdictCodeContextRadius = 30

// verdictOutcome counts what the re-check pass concluded, for the summary.
type verdictOutcome struct {
	checked   int
	stands    int
	withdrawn int
	fixed     int
	partial   int
	unclear   int
}

func (v verdictOutcome) settled() int {
	return v.withdrawn + v.fixed
}

// runThreadVerdicts re-reads every open bot thread against the code at HEAD and
// posts an explicit verdict into it.
//
// It deliberately never resolves a thread. The reviewer states a conclusion --
// the finding stands, is withdrawn, or is fixed -- and the developer decides
// what to do with it. This replaces an earlier auto-resolve that closed threads
// whenever a line within three of the anchor happened to change, which resolved
// wrong fixes and left correct ones open.
func (p *Pipeline) runThreadVerdicts(
	ctx context.Context,
	job *domain.ReviewJob,
	botComments []domain.BotUnresolvedComment,
	diffFiles []domain.DiffFile,
	llmClient domain.LLMClient,
	customPrompt *string,
	lang prompt.ResponseLanguage,
) verdictOutcome {
	var out verdictOutcome
	if !p.cfg.Review.ThreadVerdicts || len(botComments) == 0 || p.verdictAgent == nil {
		return out
	}

	changedFiles := make(map[string]bool, len(diffFiles))
	for _, f := range diffFiles {
		changedFiles[f.Path] = true
	}

	budget := p.cfg.Review.MaxVerdictThreads
	for _, bc := range botComments {
		if !needsVerdict(changedFiles[bc.FilePath], bc.LastNoteByBot) {
			slog.Debug("skipping verdict: neither the code nor the thread moved",
				"discussion_id", bc.DiscussionID, "file", bc.FilePath)
			continue
		}
		if budget <= 0 {
			slog.Warn("verdict budget exhausted; open threads left without a re-check",
				"max_verdict_threads", p.cfg.Review.MaxVerdictThreads)
			break
		}
		budget--

		code := p.readAroundLine(ctx, job.GitLabProjectID, job.HeadSHA, bc.FilePath, bc.LineNumber)
		result, err := p.verdictAgent.GenerateVerdict(ctx, llmClient, replier.VerdictInput{
			FilePath:          bc.FilePath,
			LineNumber:        bc.LineNumber,
			OriginalComment:   bc.Body,
			ThreadHistory:     bc.ThreadHistory,
			LatestCodeContext: code,
			CustomPrompt:      customPrompt,
			ResponseLanguage:  lang,
		})
		if err != nil {
			slog.Warn("failed to generate thread verdict", "discussion_id", bc.DiscussionID, "error", err)
			continue
		}
		if strings.TrimSpace(result.Reply) == "" {
			slog.Warn("thread verdict was empty; nothing posted", "discussion_id", bc.DiscussionID)
			continue
		}
		if !result.VerdictParsed {
			// No verdict means no conclusion, and a conclusion is the whole
			// point of the pass. Posting the prose alone would look like a
			// verdict without being recorded as one.
			slog.Warn("thread verdict had no parseable verdict; nothing posted",
				"discussion_id", bc.DiscussionID)
			continue
		}

		body := formatVerdictComment(result.Verdict, result.Reply, lang)
		if _, err := p.gitlabClient.PostReply(ctx, job.GitLabProjectID, job.MrIID, bc.DiscussionID, body); err != nil {
			slog.Warn("failed to post thread verdict", "discussion_id", bc.DiscussionID, "error", err)
			continue
		}

		out.checked++
		switch result.Verdict {
		case domain.VerdictStands:
			out.stands++
		case domain.VerdictWithdrawn:
			out.withdrawn++
		case domain.VerdictFixed:
			out.fixed++
		case domain.VerdictFixIncomplete:
			out.partial++
		default:
			out.unclear++
		}

		p.recordVerdictFeedback(ctx, bc, result.Verdict)
		slog.Info("thread verdict posted", "discussion_id", bc.DiscussionID,
			"file", bc.FilePath, "line", bc.LineNumber, "verdict", result.Verdict)
	}

	return out
}

// recordVerdictFeedback keeps the feedback row in step with the verdict so the
// consolidator learns from re-checks, not only from developer replies.
func (p *Pipeline) recordVerdictFeedback(ctx context.Context, bc domain.BotUnresolvedComment, verdict domain.ReplyVerdict) {
	signal := domain.SignalForVerdict(verdict, domain.FeedbackSignalNeutral)
	_, afterState := domain.ThreadStateForVerdict(nil, verdict)

	fb, err := p.feedbackStore.GetByNoteID(ctx, bc.NoteID)
	if err != nil || fb == nil {
		// Threads posted before this row existed, or by a different install.
		// The verdict still reached GitLab, which is what the developer sees.
		slog.Debug("no feedback row for verdict", "discussion_id", bc.DiscussionID, "error", err)
		return
	}
	if err := p.feedbackStore.UpdateSignal(ctx, bc.NoteID, signal, string(verdict), afterState); err != nil {
		slog.Warn("failed to record verdict feedback", "discussion_id", bc.DiscussionID, "error", err)
	}
}

// needsVerdict decides whether a thread has anything new to be said about it.
// A verdict is worth an LLM call when the file changed, or when somebody has
// replied since the reviewer last spoke. Without this, every push would add a
// comment to every open thread and the MR would fill with restatements.
func needsVerdict(fileChanged, lastNoteByBot bool) bool {
	return fileChanged || !lastNoteByBot
}

func (p *Pipeline) readAroundLine(ctx context.Context, projectID int64, sha, filePath string, centerLine int) string {
	content, err := p.gitManager.ReadFileAtSHA(ctx, projectID, sha, filePath)
	if err != nil {
		// The file may have been deleted or renamed since the finding was
		// posted. The prompt handles an empty context by answering "unclear".
		slog.Debug("could not read file for verdict", "file", filePath, "sha", sha, "error", err)
		return ""
	}

	startLine := centerLine - verdictCodeContextRadius
	if startLine < 1 {
		startLine = 1
	}
	endLine := centerLine + verdictCodeContextRadius

	var sb strings.Builder
	for i, line := range strings.Split(string(content), "\n") {
		lineNum := i + 1
		if lineNum < startLine {
			continue
		}
		if lineNum > endLine {
			break
		}
		fmt.Fprintf(&sb, "%d: %s\n", lineNum, line)
	}
	return strings.TrimRight(sb.String(), "\n")
}

func formatVerdictComment(verdict domain.ReplyVerdict, reply string, lang prompt.ResponseLanguage) string {
	return prompt.VerdictLabel(lang, string(verdict)) + "\n\n" + reply
}
