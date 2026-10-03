package httpapi

import (
	"github.com/gin-gonic/gin"
	contracts "github.com/wongyiuming/FrontierCloud/protocol"
)

// RegisterOperational exposes real metrics and the embedded, reviewed protocol
// schema. Documentation uses the same authenticated Admin session as the UI.
func RegisterOperational(router *gin.Engine, a *Admin) {
	router.GET("/metrics", func(c *gin.Context) { serveMetrics(c, a.settings.SecretsDirectory) })
	group := router.Group("", a.transport, a.authenticate)
	group.GET("/openapi.json", func(c *gin.Context) { c.Data(200, "application/json", contracts.OpenAPI) })
	group.GET("/docs", func(c *gin.Context) { c.Data(200, "text/html; charset=utf-8", []byte(swaggerHTML)) })
	group.GET("/redoc", func(c *gin.Context) { c.Data(200, "text/html; charset=utf-8", []byte(redocHTML)) })
}

const swaggerHTML = `<!doctype html><html><head><meta charset="utf-8"><title>FrontierCloud Media Service - Swagger UI</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',deepLinking:true,presets:[SwaggerUIBundle.presets.apis,SwaggerUIBundle.SwaggerUIStandalonePreset],layout:'BaseLayout'});</script></body></html>`
const redocHTML = `<!doctype html><html><head><meta charset="utf-8"><title>FrontierCloud Media Service - ReDoc</title><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><redoc spec-url="/openapi.json"></redoc><script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script></body></html>`
