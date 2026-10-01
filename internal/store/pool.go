package store

import "context"

const GiB int64 = 1024 * 1024 * 1024
const MaxStorageAllocation int64 = 10 * 1024 * GiB
const PhysicalReserve = GiB

type LocalMedia struct {
	MediaObject
	Bytes                int64
	ETag                 string
	CreatedAt, UpdatedAt int64
}
type NodePromotion struct {
	Role, Endpoint                         string
	Allocation, PhysicalUsed, PhysicalFree int64
	Media                                  []LocalMedia
}
type ResourceConfiguration struct {
	Storage struct {
		Enabled    bool  `json:"enabled"`
		Allocation int64 `json:"allocated_bytes"`
	} `json:"storage"`
	Compute struct {
		Enabled bool `json:"enabled"`
		Slots   int  `json:"worker_slots"`
	} `json:"compute"`
	Backup struct {
		Enabled bool `json:"enabled"`
	} `json:"backup"`
}
type StorageMember struct {
	ID             string         `json:"member_id"`
	RelationshipID *string        `json:"relationship_id"`
	Kind           string         `json:"member_kind"`
	Transport      string         `json:"transport"`
	Enabled        int            `json:"storage_enabled"`
	Allocation     int64          `json:"allocated_bytes"`
	Used           int64          `json:"used_bytes"`
	Reserved       int64          `json:"reserved_bytes"`
	PhysicalFree   int64          `json:"physical_free_bytes"`
	Health         string         `json:"health"`
	Writable       int            `json:"writable"`
	UpdatedAt      int64          `json:"updated_at"`
	Available      int64          `json:"available_bytes"`
	OnlineWritable int64          `json:"online_writable_bytes"`
	OfflineStored  int64          `json:"offline_stored_bytes"`
	PhysicalTotal  int64          `json:"physical_total_bytes"`
	Compute        map[string]any `json:"compute"`
	Backup         map[string]any `json:"backup"`
}
type PoolRepository interface {
	Members(context.Context) ([]StorageMember, error)
	MemberConfiguration(context.Context, string) (ResourceConfiguration, error)
	ConfigureMember(context.Context, string, ResourceConfiguration, NodeAudit) error
	AcceptFollowerConfiguration(context.Context, string, ResourceConfiguration, int64) error
	FollowerSummary(context.Context, int64, int64) (map[string]any, error)
	AdoptMasterLocal(context.Context, int64, int64, []LocalMedia) error
	Resources(context.Context, string, bool) ([]GlobalMedia, error)
	Resource(context.Context, string) (GlobalMedia, error)
	ReserveUpload(context.Context, string, string, int64, int64, AdminAudit) (UploadReservation, error)
	Upload(context.Context, string) (UploadReservation, error)
	FinalizeUpload(context.Context, string, string, int64, string, AdminAudit) (GlobalMedia, error)
	ReleaseCleanedUpload(context.Context, string, AdminAudit) error
	ExpiredUploads(context.Context, int) ([]UploadReservation, error)
	RecordGlobalPlayback(context.Context, string, string) (PlaybackResult, error)
	SetGlobalPreference(context.Context, string, int, AdminAudit) (PlaybackResult, error)
}

type GlobalMedia struct {
	ID             string  `json:"media_id"`
	MemberID       string  `json:"storage_member_id"`
	ObjectID       string  `json:"object_id"`
	Path           string  `json:"media_path"`
	Kind           string  `json:"object_kind"`
	Bytes          int64   `json:"size_bytes"`
	ETag           string  `json:"etag"`
	State          string  `json:"state"`
	CreatedAt      int64   `json:"created_at"`
	UpdatedAt      int64   `json:"updated_at"`
	RelationshipID *string `json:"relationship_id"`
	Health         string  `json:"health"`
	Transport      string  `json:"transport"`
	PlaybackStats
	HasLyrics bool `json:"has_lyrics"`
}
type UploadReservation struct {
	ID            string        `json:"upload_id"`
	MemberID      string        `json:"storage_member_id"`
	MediaID       string        `json:"media_id"`
	Path          string        `json:"media_path"`
	Kind          string        `json:"object_kind"`
	ExpectedBytes int64         `json:"expected_bytes"`
	State         string        `json:"state"`
	ExpiresAt     int64         `json:"expires_at"`
	CreatedAt     int64         `json:"created_at"`
	UpdatedAt     int64         `json:"updated_at"`
	Member        StorageMember `json:"member"`
}
