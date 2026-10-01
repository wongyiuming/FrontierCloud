package media

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func (d *Download) RemoteSingle() (store.GlobalMedia, bool) {
	if len(d.Items) == 1 && !d.Items[0].Directory && len(d.globals) == 1 && d.globals[0].RelationshipID != nil {
		return d.globals[0], true
	}
	return store.GlobalMedia{}, false
}

func (d *Download) addGlobalZIP(z *zip.Writer, row store.GlobalMedia, buffer []byte) error {
	source, err := d.globalSource(row)
	if err != nil {
		return err
	}
	defer source.Close()
	header := &zip.FileHeader{Name: row.Path, Method: zip.Store, Modified: time.Unix(row.UpdatedAt, 0).UTC()}
	header.SetMode(os.FileMode(0644))
	member, err := z.CreateHeader(header)
	if err != nil {
		return err
	}
	return d.copyGlobal(member, source, row, buffer)
}

func (d *Download) StreamRemoteSingle(writer io.Writer) error {
	row, ok := d.RemoteSingle()
	if !ok {
		return ErrPath
	}
	source, err := d.globalSource(row)
	if err != nil {
		return err
	}
	defer source.Close()
	return d.copyGlobal(writer, source, row, make([]byte, 64*1024))
}

func (d *Download) globalSource(row store.GlobalMedia) (io.ReadCloser, error) {
	if err := d.ctx.Err(); err != nil {
		return nil, err
	}
	actual, relation, err := d.service.resolveGlobal(d.ctx, row.ID, row.Path)
	if err != nil {
		return nil, err
	}
	if actual.MemberID != row.MemberID || actual.ObjectID != row.ObjectID || actual.Bytes != row.Bytes || actual.ETag != row.ETag {
		return nil, store.ErrNodeState
	}
	var source io.ReadCloser
	if relation == nil {
		object, err := d.service.repository.ObjectByID(d.ctx, row.ObjectID)
		if err != nil {
			return nil, err
		}
		if object == nil || object.Path != row.Path || object.Kind != row.Kind {
			return nil, store.ErrNodeState
		}
		f, info, err := d.Open(row.Path)
		if err != nil {
			return nil, err
		}
		if info.Size() != row.Bytes {
			f.Close()
			return nil, store.ErrNodeState
		}
		source = f
	} else {
		if d.service.control == nil {
			return nil, ErrUnavailable
		}
		source, err = d.service.control.ReadGlobalMedia(d.ctx, row)
		if err != nil {
			return nil, errors.Join(ErrUnavailable, err)
		}
	}
	return source, nil
}

func (d *Download) copyGlobal(writer io.Writer, source io.Reader, row store.GlobalMedia, buffer []byte) error {
	digest := sha256.New()
	reader := &contextReader{d.ctx, source}
	remaining := row.Bytes
	last := 0
	for remaining > 0 {
		n, err := io.ReadFull(reader, buffer[:min(int64(len(buffer)), remaining)])
		if err != nil {
			return err
		}
		digest.Write(buffer[:n])
		remaining -= int64(n)
		if remaining == 0 {
			last = n
			break
		}
		if written, err := writer.Write(buffer[:n]); err != nil {
			return err
		} else if written != n {
			return io.ErrShortWrite
		}
	}
	var extra [1]byte
	if n, err := io.ReadFull(reader, extra[:]); n != 0 || err != io.EOF {
		if err != nil {
			return err
		}
		return store.ErrNodeState
	}
	original := strings.Trim(row.ETag, `"`)
	if ownedObjectID.MatchString(original) && original != hex.EncodeToString(digest.Sum(nil)) {
		return store.ErrNodeState
	}
	// Keep just the final <=64 KiB until size/digest/EOF are proved. A failed
	// single-file stream never looks like a complete Content-Length response,
	// and a failed ZIP never gets a successful central-directory footer.
	written, err := writer.Write(buffer[:last])
	if err == nil && written != last {
		return io.ErrShortWrite
	}
	return err
}
