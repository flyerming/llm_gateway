import { rm } from 'node:fs/promises'
import { basename, dirname, relative, resolve } from 'node:path'

/**
 * Deployment-only workspace deletion policy.
 *
 * DSH's built-in delete removes only the registry record by design. This
 * wrapper keeps that API but also removes the selected directory for this
 * per-user DSH process. It never follows a path outside the user's own HOME
 * or /workspaces/<user>, and it never removes reserved state directories.
 */
export const name = 'workspace-delete-cleanup'
export const inject = ['workspaceRegistry', 'settings']

const reserved = new Set(['.dsh', 'tmp'])
const reasoningEfforts = { off: null, low: 'low', high: 'high', max: 'max' }

function userRoots() {
  const home = resolve(process.env.HOME || '')
  const cwd = resolve(process.cwd())
  return [home, cwd]
}

function deletablePath(path) {
  const target = resolve(path)
  const roots = userRoots()
  for (const root of roots) {
    const rel = relative(root, target)
    if (rel === '' || rel.startsWith('..' + '/') || rel.startsWith('../') || rel.includes('\0')) continue
    if (rel.includes('/') || rel.includes('\\')) {
      // Nested projects are allowed, but the reserved names are never targets.
      if (rel.split(/[\\/]/).some(part => reserved.has(part))) continue
    }
    if (reserved.has(basename(target))) continue
    // Never remove the user's HOME or the default workspace root itself.
    if (dirname(target) === root || rel !== '') return target
  }
  return undefined
}

function normalizePrivateModels(value) {
  if (value === null || typeof value !== 'object' || value.providers === undefined) return undefined
  const providers = value.providers
  if (providers === null || typeof providers !== 'object') return undefined
  let changed = false
  const nextProviders = { ...providers }
  for (const [providerId, provider] of Object.entries(providers)) {
    if (provider === null || typeof provider !== 'object' || !Array.isArray(provider.models)) continue
    const models = provider.models.map(model => {
      if (model === null || typeof model !== 'object' || model.reasoningEfforts === false) return model
      if (model.reasoningEfforts !== undefined) return model
      const compat = { ...(model.compat ?? {}) }
      if (compat.supportsReasoningEffort === false) return model
      compat.supportsReasoningEffort = true
      compat.thinkingFormat ??= process.env.DSH_MODEL_REASONING_FORMAT || 'openai'
      changed = true
      return {
        ...model,
        compat,
        reasoningEfforts: { ...reasoningEfforts },
      }
    })
    if (models.some((model, index) => model !== provider.models[index])) {
      nextProviders[providerId] = {
        ...provider,
        reasoning: provider.reasoning ?? 'high',
        models,
      }
    }
  }
  return changed ? { ...value, providers: nextProviders } : undefined
}

export function apply(ctx) {
  const registry = ctx.workspaceRegistry
  let normalizing = false
  const normalizeSettings = async next => {
    if (normalizing) return
    const patched = normalizePrivateModels(next)
    if (patched === undefined) return
    normalizing = true
    try {
      await ctx.settings.update('llm-pi-ai', { providers: patched.providers })
    } finally {
      normalizing = false
    }
  }
  ctx.on('settings/updated', (namespace, next) => {
    if (namespace === 'llm-pi-ai') void normalizeSettings(next)
  })
  void normalizeSettings(ctx.settings.get('llm-pi-ai'))

  const originalDelete = registry.delete.bind(registry)
  registry.delete = async id => {
    const workspace = registry.get(id)
    const path = workspace?.path
    const result = await originalDelete(id)
    if (!result || path === undefined) return result

    const target = deletablePath(path)
    if (target === undefined) {
      console.warn(`workspace-delete-cleanup: refused path ${JSON.stringify(path)}`)
      return result
    }
    try {
      await rm(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
      console.info(`workspace-delete-cleanup: removed ${target}`)
    } catch (error) {
      // Registry deletion already succeeded; do not turn a cleanup failure into
      // a misleading Remote error. The next cleanup/reconciliation can retry.
      console.error(`workspace-delete-cleanup: failed to remove ${target}`, error)
    }
    return result
  }
}
