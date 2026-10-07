package store

import "context"

// This is a read-only logical diagnostic, not a restore/adoption permission.
// A lease only fences native writers sharing the same local data volume.
type MaintenanceSnapshot struct {
	SchemaGeneration       int              `json:"schema_generation"`
	Role                   string           `json:"role"`
	Pending                map[string]int64 `json:"pending"`
	CompletedNativeRenames int64            `json:"completed_native_renames"`
	LogicalIdle            bool             `json:"logical_idle"`
}

type MaintenanceRepository interface {
	InspectMaintenance(context.Context) (MaintenanceSnapshot, error)
	OwnedAdoptionObjects(context.Context, string) ([]MediaObject, error)
	AdoptOwnedStorage(context.Context, string, string, []LocalMedia, NodeAudit) (int, error)
	DrainCompletedRenames(context.Context, string, NodeAudit) (int, error)
	ExportRecordingInventory(context.Context, string, int64) (RecordingInventory, error)
	AdoptOwnedRecordings(context.Context, SignedRecordingInventory, string, NodeAudit) (int, error)
}
