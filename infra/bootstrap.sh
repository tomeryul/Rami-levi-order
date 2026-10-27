#!/usr/bin/env bash
# One-time setup. Run in Azure Cloud Shell (Bash) from the repo root:
#   git clone https://github.com/<you>/rami-levy-mcp && cd rami-levy-mcp
#   GITHUB_REPO=<you>/rami-levy-mcp bash infra/bootstrap.sh
#
# Creates (region israelcentral): resource group, ACR, storage + file share,
# Key Vault (RBAC), user-assigned identity, Container Apps env + app,
# Entra app for OAuth (only you can sign in), and a GitHub OIDC deploy identity.
set -euo pipefail

: "${GITHUB_REPO:?set GITHUB_REPO=owner/repo}"
LOC="${LOC:-israelcentral}"
RG="${RG:-rg-ramilevy-mcp}"
SUFFIX="${SUFFIX:-$(openssl rand -hex 3)}"
ACR="acrramilevy${SUFFIX}"
SA="stramilevy${SUFFIX}"
KV="kv-ramilevy-${SUFFIX}"
UAMI="id-ramilevy-mcp"
ENV_NAME="cae-ramilevy"
APP="ramilevy-mcp"
SHARE="data"

SUB_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
ME_OID=$(az ad signed-in-user show --query id -o tsv)

echo "== Resource group / ACR / storage / Key Vault / identity"
az group create -n "$RG" -l "$LOC" -o none
az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled false -o none
az storage account create -g "$RG" -n "$SA" -l "$LOC" --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 -o none
az storage share-rm create -g "$RG" --storage-account "$SA" -n "$SHARE" --quota 5 -o none
az keyvault create -g "$RG" -n "$KV" -l "$LOC" --enable-rbac-authorization true -o none
KV_ID=$(az keyvault show -n "$KV" --query id -o tsv)
az role assignment create --assignee-object-id "$ME_OID" --assignee-principal-type User --role "Key Vault Secrets Officer" --scope "$KV_ID" -o none
az identity create -g "$RG" -n "$UAMI" -l "$LOC" -o none
UAMI_ID=$(az identity show -g "$RG" -n "$UAMI" --query id -o tsv)
UAMI_PID=$(az identity show -g "$RG" -n "$UAMI" --query principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)
az role assignment create --assignee-object-id "$UAMI_PID" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" -o none
az role assignment create --assignee-object-id "$UAMI_PID" --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" --scope "$KV_ID" -o none

echo "== Container Apps environment"
az containerapp env create -g "$RG" -n "$ENV_NAME" -l "$LOC" -o none
SA_KEY=$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv)
az containerapp env storage set -g "$RG" -n "$ENV_NAME" --storage-name data --storage-type AzureFile \
  --azure-file-account-name "$SA" --azure-file-account-key "$SA_KEY" --azure-file-share-name "$SHARE" --access-mode ReadWrite -o none
ENV_ID=$(az containerapp env show -g "$RG" -n "$ENV_NAME" --query id -o tsv)
FQDN="${APP}.$(az containerapp env show -g "$RG" -n "$ENV_NAME" --query properties.defaultDomain -o tsv)"
BASE_URL="https://${FQDN}"

echo "== Entra app for the connector's OAuth ($BASE_URL)"
OAUTH_APP_ID=$(az ad app create --display-name "Rami Levy MCP" --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "${BASE_URL}/auth/callback" --query appId -o tsv)
OAUTH_OBJ=$(az ad app show --id "$OAUTH_APP_ID" --query id -o tsv)
SCOPE_ID=$(cat /proc/sys/kernel/random/uuid)
az ad app update --id "$OAUTH_APP_ID" --identifier-uris "api://${OAUTH_APP_ID}"
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/${OAUTH_OBJ}" --headers "Content-Type=application/json" --body "{
  \"api\": {
    \"requestedAccessTokenVersion\": 2,
    \"oauth2PermissionScopes\": [{
      \"id\": \"${SCOPE_ID}\", \"value\": \"access\", \"type\": \"User\", \"isEnabled\": true,
      \"adminConsentDisplayName\": \"Use Rami Levy connector\", \"adminConsentDescription\": \"Use Rami Levy connector\",
      \"userConsentDisplayName\": \"Use Rami Levy connector\", \"userConsentDescription\": \"Use Rami Levy connector\"
    }]
  }
}"
az ad sp create --id "$OAUTH_APP_ID" -o none
OAUTH_SP=$(az ad sp show --id "$OAUTH_APP_ID" --query id -o tsv)
# Only explicitly assigned users can sign in — assign just you.
az ad sp update --id "$OAUTH_SP" --set appRoleAssignmentRequired=true
az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${OAUTH_SP}/appRoleAssignedTo" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"${ME_OID}\",\"resourceId\":\"${OAUTH_SP}\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}" -o none
OAUTH_SECRET=$(az ad app credential reset --id "$OAUTH_APP_ID" --append --display-name aca --years 2 --query password -o tsv)

echo "== Secrets into Key Vault (RBAC can take a minute to apply)"
sleep 45
read -rp  "Rami Levy email: " RL_EMAIL
read -rsp "Rami Levy password: " RL_PASS; echo
az keyvault secret set --vault-name "$KV" -n rami-levy-email --value "$RL_EMAIL" -o none
az keyvault secret set --vault-name "$KV" -n rami-levy-password --value "$RL_PASS" -o none
az keyvault secret set --vault-name "$KV" -n entra-client-secret --value "$OAUTH_SECRET" -o none
az keyvault secret set --vault-name "$KV" -n jwt-signing-key --value "$(openssl rand -hex 32)" -o none
unset RL_PASS OAUTH_SECRET

echo "== First image build (in ACR)"
az acr build -r "$ACR" -t "${APP}:init" . -o none

echo "== Container App"
KVU="https://${KV}.vault.azure.net/secrets"
cat > /tmp/app.yaml <<EOF
location: ${LOC}
identity:
  type: UserAssigned
  userAssignedIdentities:
    ${UAMI_ID}: {}
properties:
  managedEnvironmentId: ${ENV_ID}
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8000
      transport: auto
      allowInsecure: false
    registries:
      - server: ${ACR}.azurecr.io
        identity: ${UAMI_ID}
    secrets:
      - { name: rl-email,      keyVaultUrl: ${KVU}/rami-levy-email,     identity: ${UAMI_ID} }
      - { name: rl-password,   keyVaultUrl: ${KVU}/rami-levy-password,  identity: ${UAMI_ID} }
      - { name: entra-secret,  keyVaultUrl: ${KVU}/entra-client-secret, identity: ${UAMI_ID} }
      - { name: jwt-key,       keyVaultUrl: ${KVU}/jwt-signing-key,     identity: ${UAMI_ID} }
  template:
    containers:
      - name: app
        image: ${ACR}.azurecr.io/${APP}:init
        resources: { cpu: 1.0, memory: 2Gi }
        env:
          - { name: RAMI_LEVY_EMAIL,     secretRef: rl-email }
          - { name: RAMI_LEVY_PASSWORD,  secretRef: rl-password }
          - { name: ENTRA_CLIENT_SECRET, secretRef: entra-secret }
          - { name: JWT_SIGNING_KEY,     secretRef: jwt-key }
          - { name: ENTRA_CLIENT_ID,     value: "${OAUTH_APP_ID}" }
          - { name: ENTRA_TENANT_ID,     value: "${TENANT_ID}" }
          - { name: PUBLIC_BASE_URL,     value: "${BASE_URL}" }
          - { name: DEBUG_TOOLS,         value: "1" }
        volumeMounts:
          - { volumeName: data, mountPath: /data }
        probes:
          - type: Liveness
            httpGet: { path: /healthz, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 30
    scale:
      minReplicas: 0
      maxReplicas: 1
    volumes:
      - { name: data, storageType: AzureFile, storageName: data }
EOF
az containerapp create -g "$RG" -n "$APP" --yaml /tmp/app.yaml -o none

echo "== GitHub Actions deploy identity (OIDC, no secrets)"
GH_APP_ID=$(az ad app create --display-name "gh-deploy-ramilevy-mcp" --query appId -o tsv)
az ad sp create --id "$GH_APP_ID" -o none
GH_SP=$(az ad sp show --id "$GH_APP_ID" --query id -o tsv)
az ad app federated-credential create --id "$GH_APP_ID" --parameters "{
  \"name\": \"main\", \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${GITHUB_REPO}:ref:refs/heads/main\", \"audiences\": [\"api://AzureADTokenExchange\"]}" -o none
RG_ID=$(az group show -n "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$GH_SP" --assignee-principal-type ServicePrincipal --role Contributor --scope "$RG_ID" -o none
az role assignment create --assignee-object-id "$GH_SP" --assignee-principal-type ServicePrincipal --role AcrPush --scope "$ACR_ID" -o none

cat <<EOF

================ DONE ================
Connector URL (add in Claude → Settings → Connectors → Add custom connector):
  ${BASE_URL}/mcp

GitHub repo → Settings → Secrets and variables → Actions → Variables:
  AZURE_CLIENT_ID=${GH_APP_ID}
  AZURE_TENANT_ID=${TENANT_ID}
  AZURE_SUBSCRIPTION_ID=${SUB_ID}
  ACR_NAME=${ACR}
  RESOURCE_GROUP=${RG}
  CONTAINER_APP=${APP}
EOF
