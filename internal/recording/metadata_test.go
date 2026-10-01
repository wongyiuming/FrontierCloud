package recording

import (
	"bytes"
	"encoding/binary"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"strings"
	"testing"
)

func trailer(raw string) []byte {
	v := append([]byte("opaque audio"), []byte(raw)...)
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(raw)))
	v = append(v, length[:]...)
	return append(v, []byte(TrailerMagic)...)
}
func TestMetadataPythonFooterBoundedAndStrict(t *testing.T) {
	data := trailer(`{"version":1,"title":"现场录音","lyrics":[{"time":1.25,"text":"雪花❄"}]}`)
	v := Metadata(bytes.NewReader(data), int64(len(data)))
	if v == nil || v.Title != "现场录音" || len(v.Lyrics) != 1 || v.Lyrics[0].Time != 1.25 {
		t.Fatal(v)
	}
	for _, raw := range []string{`{"version":true}`, `{"version":1,"title":null,"lyrics":[{"time":null,"text":"a"}]}`, `{"version":1,"lyrics":[{"time":true,"text":"a"}]}`, `{"version":1,"lyrics":[{"time":-1,"text":"a"}]}`, `{"version":1,"lyrics":[{"time":1e99999,"text":"a"}]}`, `{"version":1,"lyrics":[{"time":0,"text":true}]}`, `{"version":1} {}`, `{"version":2}`, `{"version":1,"title":"` + string([]byte{255}) + `"}`, `{"version":1,"lyrics":[{"time":0,"text":"` + strings.Repeat("字", 4001) + `"}]}`} {
		data := trailer(raw)
		if Metadata(bytes.NewReader(data), int64(len(data))) != nil {
			t.Fatal("invalid footer accepted", raw[:min(len(raw), 90)])
		}
	}
	data = trailer(strings.Repeat(" ", store.MaxRecordingMetadata+1))
	if Metadata(bytes.NewReader(data), int64(len(data))) != nil {
		t.Fatal("large footer")
	}
	data = trailer(`{"version":1}`)
	binary.BigEndian.PutUint64(data[len(data)-8-len(TrailerMagic):], ^uint64(0))
	if Metadata(bytes.NewReader(data), int64(len(data))) != nil {
		t.Fatal("overflow footer")
	}
	if Metadata(bytes.NewReader([]byte("plain")), 5) != nil {
		t.Fatal("plain audio footer")
	}
}
