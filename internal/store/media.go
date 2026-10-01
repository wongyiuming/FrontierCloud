package store

import "context"

// MediaObject is the durable identity of a file. Paths are locators, not IDs.
type MediaObject struct {
	ID   string `json:"media_id"`
	Path string `json:"media_path"`
	Kind string `json:"object_kind"`
}

type PlaybackStats struct {
	PlayScore  int64 `json:"play_score"`
	Preference int   `json:"preference"`
}

type PlaybackResult struct {
	PlaybackStats
	MediaID   string  `json:"media_id"`
	Counted   bool    `json:"counted"`
	Threshold float64 `json:"threshold_seconds"`
}

type LyricRelation struct {
	Track string `json:"track"`
	Lyric string `json:"lyric"`
}
type LyricPair struct {
	Track MediaObject
	Lyric MediaObject
}
type AutoLyricResult struct {
	Linked    int `json:"linked"`
	Preserved int `json:"preserved"`
	Ambiguous int `json:"ambiguous"`
	Unmatched int `json:"unmatched"`
}

// MediaRepository owns transactions and dialect selection. HTTP and services
// never receive a SQL connection or choose a database-specific query.
type MediaRepository interface {
	MediaDeletionRepository
	EnsureObjects(context.Context, []MediaObject) (map[string]string, error)
	ObjectByID(context.Context, string) (*MediaObject, error)
	HiddenPaths(context.Context) (map[string]bool, error)
	Stats(context.Context, []string) (map[string]PlaybackStats, error)
	DirectoryPreferences(context.Context) (map[string]int, error)
	RecordPlayback(context.Context, MediaObject, string) (PlaybackResult, error)
	LyricPath(context.Context, string) (string, error)
	BindLyric(context.Context, MediaObject, MediaObject) error
	SetPreference(context.Context, MediaObject, int, AdminAudit) (PlaybackResult, error)
	SetHidden(context.Context, []string, bool, AdminAudit) error
	CompleteUpload(context.Context, MediaObject, string, AdminAudit) error
	CheckRename(context.Context, string) error
	CompleteRename(context.Context, string, string, string, AdminAudit) error
	LyricRelations(context.Context, string, string) ([]LyricRelation, error)
	ReplaceLyricRelations(context.Context, MediaObject, []MediaObject, MediaObject, AdminAudit) (int, error)
	AutoLyricRelations(context.Context, []LyricPair, AutoLyricResult, AdminAudit) (AutoLyricResult, error)
}

type NodeIdentity struct {
	ID         string
	Role       string
	Endpoint   string
	PrivateKey string // encrypted with the persistent node vault
	CreatedAt  int64
}
