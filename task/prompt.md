## Railway Service Configuration — Environment Variable Override

The `railway.toml` has been updated to remove the global `startCommand` so each service can have its own start configuration.

To complete the setup, the founder must configure the `peaceful-peace` worker service manually in the Railway dashboard:

### Steps:

1. Go to [Railway Dashboard](https://railway.app)
2. Navigate to the **peaceful-peace** service
3. Open the **Variables** tab
4. Add a new environment variable:
   - **Key:** `RAILWAY_START_COMMAND`
   - **Value:** `python3 -m workers.scheduler`
5. Click "Save"
6. Trigger a new deployment (Railway will pick up the new start command)

### How It Works:

- **railway.toml** now contains only build and deploy metadata (no global startCommand)
- Each Railway service can override the start command via the `RAILWAY_START_COMMAND` environment variable
- This is the [official Railway approach](https://docs.railway.app/guides/variables) for per-service customization
- The **pricebot** service will use its dashboard start command: `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
- The **peaceful-peace** service will use `RAILWAY_START_COMMAND` to run the scheduler

### Why This Approach:

- ✅ No file changes needed after initial railway.toml commit
- ✅ Easy to toggle/modify per-service without CI/CD complexity
- ✅ Each service remains independent and can be deployed separately
- ✅ Official Railway pattern for multi-service deployments

### Verification:

After setting the variable and deploying, check the service logs:
```bash
railway logs --service peaceful-peace --tail 20
```

You should see logs from the scheduler (APScheduler startup), not the API.
