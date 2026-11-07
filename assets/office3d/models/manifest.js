// Deliberately empty model URLs: the reference images are art direction, not
// redistributable rigged 3D assets. Never replace a missing human with a mascot.
export const REQUIRED_MODEL_CLIPS = Object.freeze([
  'WORKING', 'IDLE', 'SEATED_IDLE', 'WALKING', 'SOCIAL',
  'SIT_DOWN', 'STAND_UP', 'COFFEE', 'POOL', 'VISITING',
]);

const model = (name, gender, appearance, height) => Object.freeze({
  name, gender, appearance, height,
  url: '',
  license: null,
  referenceReviewed: false,
  forwardRotation: 0,
  headBone: '',
  clips: Object.freeze(Object.fromEntries(REQUIRED_MODEL_CLIPS.map(state => [state, '']))),
});

export const AVATAR_MODELS = Object.freeze({
  market: model('HERMES AGENT', 'male', 'Black suit, ivory shirt, teal tie; swept black hair, clean-shaven face.', 1.82),
  neuro: model('NEUROBRO', 'female', 'Black fitted blazer and skirt, ivory blouse, black heels; long side-parted dark hair.', 1.70),
  risk: model('RISK MANAGER', 'female', 'Black fitted blazer and skirt, ivory blouse, black heels; dark bob haircut.', 1.70),
  trading: model('BINANCE', 'female', 'Black fitted blazer and skirt, ivory blouse, black heels; dark high ponytail.', 1.70),
  boss: model('HARUN', 'male', 'Black suit, ivory shirt, black tie; swept black hair and light stubble.', 1.82),
});

export function avatarAssetReadiness() {
  const models = Object.entries(AVATAR_MODELS).map(([role, entry]) => ({
    role, name: entry.name, configured: Boolean(entry.url),
    referenceReviewed: entry.referenceReviewed,
    licenseRecorded: Boolean(entry.license?.file && entry.license?.sourceUrl),
    headBoneConfigured: Boolean(entry.headBone?.trim()),
    missingClips: REQUIRED_MODEL_CLIPS.filter(state => !entry.clips[state]),
  }));
  return { ready: models.every(model => model.configured && model.referenceReviewed &&
    model.licenseRecorded && model.headBoneConfigured && model.missingClips.length === 0), models };
}
