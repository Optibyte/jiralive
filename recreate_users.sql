CREATE TABLE "users" (
    "id" TEXT NOT NULL,
    "tracking_id" TEXT,
    "auth0_id" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "full_name" TEXT NOT NULL,
    "role" "UserRole" NOT NULL,
    "job_role" TEXT,
    "avatar_url" TEXT,
    "timezone" TEXT NOT NULL DEFAULT 'UTC',
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "is_active" BOOLEAN NOT NULL DEFAULT true,
    "last_login_at" TIMESTAMP(3),
    "jira_account_id" TEXT,
    "org_id" TEXT,
    "market_id" TEXT,
    "account_id" TEXT,
    "project_id" TEXT,
    "team_id" TEXT,
    CONSTRAINT "users_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "users_tracking_id_key" ON "users"("tracking_id");
CREATE UNIQUE INDEX "users_auth0_id_key" ON "users"("auth0_id");
CREATE UNIQUE INDEX "users_email_key" ON "users"("email");
