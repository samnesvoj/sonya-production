/**
 * Contract every config backend must implement. Stage 1 (this prototype)
 * ships only LocalDraftStore. Stage 2 (see PRODUCTION_INTEGRATION_PLAN.md)
 * is expected to add an HttpConfigStore that talks to the real backend
 * without any change to ClothStudio, since Studio only ever depends on this
 * interface.
 *
 * @typedef {import('./default-config.js').createDefaultConfig extends () => infer C ? C : never} ClothConfig
 */
export class ConfigStore {
  /** @returns {Promise<{ config: ClothConfig, textureUrl: string|null } | null>} */
  async load() {
    throw new Error('ConfigStore.load() not implemented');
  }

  /** @param {ClothConfig} config */
  async save(config) {
    throw new Error('ConfigStore.save() not implemented');
  }

  /**
   * @param {File} file
   * @returns {Promise<string>} a URL usable as texture.url
   */
  async uploadTexture(file) {
    throw new Error('ConfigStore.uploadTexture() not implemented');
  }

  /** @returns {Promise<void>} */
  async reset() {
    throw new Error('ConfigStore.reset() not implemented');
  }
}
