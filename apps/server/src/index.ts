import express from 'express';

const app = express();
const port = Number(process.env.PORT ?? 3000);

app.get('/api/health', (_request, response) => {
  response.json({ status: 'ok' });
});

app.listen(port, () => {
  console.log(`服务端已启动：http://localhost:${port}`);
});
