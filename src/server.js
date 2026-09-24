import http from 'node:http';
import { createApp } from './app.js';

const port = Number(process.env.PORT || 8080);
const app = createApp();
const server = http.createServer(app.handle);

server.listen(port, () => {
  // eslint-disable-next-line no-console
  console.log(`Tide scheduling service listening on port ${port}`);
});

export { server };
