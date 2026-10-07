package business

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// CompleteMasterUpload is only called with a durable filesystem publication
// proof. Local identity, global placement and capacity are one transaction.
func (r *Repository) CompleteMasterUpload(ctx context.Context, operation string, o store.MediaObject, size int64, etag string, free int64, a store.AdminAudit) error {
	if !nodeIDPattern.MatchString(operation) || !nodeHashPattern.MatchString(o.ID) || !poolPath(store.LocalMedia{MediaObject: o, Bytes: size, ETag: etag}) || size <= 0 || free < 0 || etag == "" || strings.ContainsAny(etag, "\r\n\x00") {
		return nodeConflict("invalid Master upload publication")
	}
	return r.write(ctx, func(q queryer) error {
		n, err := readNode(ctx, q, r.lock())
		if err != nil {
			return err
		}
		if n.Role != "Master" {
			return nodeConflict("only Master publishes local pool uploads")
		}
		v, err := scanUpload(q.QueryRowContext(ctx, "SELECT "+uploadColumns+" FROM cluster_upload_sessions WHERE upload_id=?"+r.lock(), operation))
		if err != nil {
			return err
		}
		if v.MemberID != n.ID || v.MediaID != o.ID || v.Path != o.Path || v.Kind != o.Kind || v.ExpectedBytes != size {
			return nodeConflict("Master publication differs from reservation")
		}
		var id, name, kind string
		err = q.QueryRowContext(ctx, "SELECT media_id,media_path,object_kind FROM media_objects WHERE media_id=? OR path_locator=?"+r.lock(), o.ID, locator(o.Path)).Scan(&id, &name, &kind)
		if err == nil {
			if v.State != "complete" || id != o.ID || name != o.Path || kind != o.Kind {
				return nodeConflict("Master publication target occupied")
			}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return err
		} else {
			if v.State != "reserved" {
				return nodeConflict("Master publication identity missing")
			}
			stamp := timestamp(time.Now())
			if _, err = q.ExecContext(ctx, "INSERT INTO media_objects(media_id,object_kind,media_path,path_locator,created_at,updated_at) VALUES (?,?,?,?,?,?)", o.ID, o.Kind, o.Path, locator(o.Path), stamp, stamp); err != nil {
				return err
			}
		}
		var result store.GlobalMedia
		if err = r.finalizeUpload(ctx, q, operation, o.ID, size, etag, a, true, &result); err != nil {
			return err
		}
		_, err = q.ExecContext(ctx, "UPDATE cluster_storage_members SET physical_free_bytes=?,updated_at=? WHERE member_id=?", free, time.Now().Unix(), n.ID)
		return err
	})
}
