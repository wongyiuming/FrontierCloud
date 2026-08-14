FROM nginx:1.26-alpine

# 删除默认配置
RUN rm /etc/nginx/conf.d/default.conf

# 注入env必须用template
COPY nginx.conf /etc/nginx/templates/nginx.conf.template

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]