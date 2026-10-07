package httpapi

import (
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"net/url"
	"os"
	"path"
	"strings"
)

func RegisterKaraokeMedia(router *gin.Engine, public *Public, resolver *network.Resolver) {
	resolve := func(c *gin.Context) (media.KaraokeMedia, bool) {
		noStore(c)
		tokens := c.Request.URL.Query()["media"]
		if len(tokens) != 1 {
			invalid(c, "query", "media")
			return media.KaraokeMedia{}, false
		}
		v, e := public.media.ResolveKaraoke(c.Request.Context(), tokens[0])
		if e != nil {
			if errors.Is(e, media.ErrUnavailable) {
				detail(c, 503, e.Error())
			} else if errors.Is(e, os.ErrNotExist) {
				detail(c, 404, "Karaoke media not found")
			} else {
				internalError(c, e)
			}
			return v, false
		}
		if v.Kind == "global" && (!public.settings.TLSEnabled || !resolver.SecureAdmin(c.Request)) {
			detail(c, 403, "Karaoke global media requires HTTPS")
			return v, false
		}
		return v, true
	}
	router.GET("/api/v1/karaoke/context", func(c *gin.Context) {
		v, ok := resolve(c)
		if !ok {
			return
		}
		query := url.Values{"media": {c.Query("media")}}.Encode()
		var lyrics any
		if v.HasLyrics {
			lyrics = "/api/v1/karaoke/lyrics?" + query
		}
		c.JSON(200, gin.H{"id": c.Query("media"), "title": strings.TrimSuffix(path.Base(v.Path), path.Ext(v.Path)), "type": v.Type, "stream_url": "/api/v1/karaoke/stream?" + query, "has_lyrics": v.HasLyrics, "lyrics_url": lyrics, "cover_url": "/favicon.ico"})
	})
	serve := func(c *gin.Context) {
		v, ok := resolve(c)
		if !ok {
			return
		}
		id := ""
		if v.Kind == "global" {
			id = v.ID
		}
		c.Set("karaoke_no_store", true)
		public.deliver(c, v.Path, id)
	}
	router.GET("/api/v1/karaoke/stream", serve)
	router.HEAD("/api/v1/karaoke/stream", serve)
	router.GET("/api/v1/karaoke/lyrics", func(c *gin.Context) {
		v, ok := resolve(c)
		if !ok {
			return
		}
		if !v.HasLyrics {
			detail(c, 404, "Lyrics not found")
			return
		}
		entries, e := public.media.KaraokeLyrics(c.Request.Context(), v)
		if e != nil {
			internalError(c, e)
			return
		}
		c.JSON(200, gin.H{"entries": entries})
	})
}
