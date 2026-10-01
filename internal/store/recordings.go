package store

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"unicode/utf8"
)

var ErrRecordingMissing = errors.New("录音不存在")
var ErrRecordingState = errors.New("录音状态或存储配额已变化")
var ErrRecordingQuota = errors.New("个人录音空间不足")

const MaxRecordingBytes int64 = GiB
const MaxRecordingMetadata = 2 * 1024 * 1024

type RecordingLyric struct {
	Time float64 `json:"time"`
	Text string  `json:"text"`
}
type RecordingMetadata struct {
	Title  string           `json:"title"`
	Lyrics []RecordingLyric `json:"lyrics"`
}
type Recording struct {
	ID          string           `json:"recording_id"`
	UserID      string           `json:"-"`
	MemberID    string           `json:"-"`
	Filename    string           `json:"filename"`
	ContentType string           `json:"content_type"`
	Bytes       int64            `json:"size_bytes"`
	SHA256      *string          `json:"sha256"`
	State       string           `json:"-"`
	Title       string           `json:"title"`
	Lyrics      []RecordingLyric `json:"lyrics"`
	CreatedAt   int64            `json:"created_at"`
	UpdatedAt   int64            `json:"-"`
}
type RecordingReceipt struct {
	ID       string             `json:"recording_id"`
	Bytes    int64              `json:"size_bytes"`
	SHA256   string             `json:"sha256"`
	Metadata *RecordingMetadata `json:"metadata,omitempty"`
}

func RecordingContentType(v string) bool {
	switch v {
	case "audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "application/octet-stream":
		return true
	}
	return false
}
func ValidRecordingMetadata(v RecordingMetadata) bool {
	if !utf8.ValidString(v.Title) || utf8.RuneCountInString(v.Title) > 255 || len(v.Lyrics) > 10000 {
		return false
	}
	for _, line := range v.Lyrics {
		if !utf8.ValidString(line.Text) || utf8.RuneCountInString(line.Text) > 4000 || line.Time < 0 || math.IsNaN(line.Time) || math.IsInf(line.Time, 0) {
			return false
		}
	}
	raw, err := json.Marshal(v)
	return err == nil && len(raw) <= MaxRecordingMetadata
}
func ValidRecordingFilename(v string) bool {
	return utf8.ValidString(v) && utf8.RuneCountInString(v) > 0 && utf8.RuneCountInString(v) <= 255 && !strings.ContainsAny(v, "\r\n\x00/\\")
}

// Completion methods require a verified physical receipt / cleanup proof.
// A timeout, expired ticket or client-provided hash never constitutes proof.
type RecordingRepository interface {
	Recording(context.Context, string) (*Recording, error)
	ListRecordings(context.Context, string, string, int) ([]Recording, error)
	ReserveRecording(context.Context, Recording, int64, KaraokeAudit) (Recording, StorageMember, error)
	FinalizeRecording(context.Context, string, string, RecordingReceipt, KaraokeAudit) error
	StageRecordingDeletion(context.Context, string, string, bool, KaraokeAudit) (*Recording, error)
	CompleteRecordingDeletion(context.Context, string, string, KaraokeAudit) error
	PendingRecordingDeletions(context.Context, int) ([]Recording, error)
	DeferRecordingDeletion(context.Context, string) error
	StageExpiredRecordings(context.Context, int) error
	ReserveOwnedRecording(context.Context, string, Recording, int64, NodeAudit) error
	CheckLocalRecordingUpload(context.Context, Recording) error
	StageOwnedRecordingDeletion(context.Context, string, string, string) (*Recording, error)
	StageOwnedUserDeletion(context.Context, string, string) error
	CompleteOwnedRecording(context.Context, string, RecordingReceipt, int64, NodeAudit) error
	CompleteOwnedRecordingDeletion(context.Context, string, string, string, int64, NodeAudit) error
}
