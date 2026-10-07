// Package store defines runtime-independent persistence boundaries.
package store

import "context"

// Store is the common lifecycle contract for a selected authoritative backend.
// Business repositories will extend this boundary by domain rather than expose
// raw SQL or a generic ORM to handlers.
type Store interface {
	Backend() string
	Ping(context.Context) error
	Initialize(context.Context) error
	Close() error
	Media() MediaRepository
	Nodes() NodeRepository
	Admin() AdminRepository
	Security() SecurityRepository
	Observations() ObservationRepository
	Pool() PoolRepository
	Karaoke() KaraokeRepository
	Recordings() RecordingRepository
	Backups() BackupRepository
	Maintenance() MaintenanceRepository
}
