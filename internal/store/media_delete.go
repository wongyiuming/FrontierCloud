package store

import "context"

type DeleteItem struct {
	Path      string `json:"relative_path"`
	Slot      string `json:"slot"`
	Directory bool   `json:"is_directory"`
	OwnedID   string `json:"owned_object_id,omitempty"`
	Bytes     int64  `json:"owned_bytes,omitempty"`
}
type DeleteOperation struct {
	ID    string
	State string
	Items []DeleteItem
}
type MediaDeletionRepository interface {
	PrepareDelete(context.Context, DeleteOperation, AdminAudit) error
	CommitDelete(context.Context, string, AdminAudit) error
	DeleteOperations(context.Context) ([]DeleteOperation, error)
	DeleteOperation(context.Context, string) (*DeleteOperation, error)
	ForgetDelete(context.Context, string) error
}
