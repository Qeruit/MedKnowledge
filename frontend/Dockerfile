FROM node:22.18-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

FROM nginx:1.29-alpine
COPY --from=build /src/dist /usr/share/nginx/html
COPY deploy/nginx.routes.conf /etc/nginx/snippets/medknowledge-routes.conf
COPY deploy/nginx.http.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
