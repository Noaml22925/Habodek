package replier

import (
	"testing"

	"habodek/internal/domain"
)

func TestParseReplyResultAcceptsAFencedVerdict(t *testing.T) {
	// The re-check pass posts nothing when it cannot read a verdict, so the
	// parser has to survive the code fence models habitually wrap JSON in.
	got := parseReplyResult("```json\n{\"verdict\": \"withdrawn\", \"reply\": \"You are right, the lock is held.\"}\n```")

	if !got.VerdictParsed {
		t.Fatal("a fenced JSON verdict should parse")
	}
	if got.Verdict != domain.VerdictWithdrawn {
		t.Errorf("verdict = %q, want withdrawn", got.Verdict)
	}
	if got.Reply != "You are right, the lock is held." {
		t.Errorf("reply = %q, want the prose only", got.Reply)
	}
}

func TestParseReplyResultKeepsProseWhenTheModelIgnoresTheFormat(t *testing.T) {
	// A model that answers in plain prose still produces a usable comment; it
	// just does not get to record a verdict.
	got := parseReplyResult("This still looks wrong to me: the lock is released before the write.")

	if got.VerdictParsed {
		t.Error("plain prose must not be reported as a parsed verdict")
	}
	if got.Reply == "" {
		t.Error("prose must survive so the reply pipeline can still post it")
	}
}

func TestParseReplyResultRejectsAnUnknownVerdict(t *testing.T) {
	// An invented verdict must not be recorded as if it were one of ours, but
	// the reply is still worth posting.
	got := parseReplyResult(`{"verdict": "mostly_fine", "reply": "Looks acceptable."}`)

	if got.VerdictParsed {
		t.Error("an unknown verdict string must not count as parsed")
	}
	if got.Reply != "Looks acceptable." {
		t.Errorf("reply = %q, want the prose extracted from the JSON", got.Reply)
	}
}
