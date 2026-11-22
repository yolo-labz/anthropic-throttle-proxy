// Load after the fleet parity extension. This never selects or switches a model.
import {open} from 'node:fs/promises';
import {constants} from 'node:fs';
import {SharedTokenAdmission, withOpenAIAPIAdmission} from './admission.mjs';

export default function install(pi) {
  let admission;
  pi.on('session_start', async (_event, ctx) => {
    const provider = ctx.modelRegistry.getProvider('openai-api');
    // Pi reports extension errors and continues startup. Block this exact API
    // model first so missing/bad configuration cannot silently bypass admission.
    const unavailable = {...provider};
    for (const method of ['stream', 'streamSimple']) unavailable[method] = (model, ...args) => {
      if (model.id === 'gpt-6.2-astra') throw new Error('OpenAI org token admission unavailable');
      return provider[method](model, ...args);
    };
    pi.registerProvider(unavailable);
    const path = process.env.PI_OPENAI_TOKEN_ADMISSION_CONFIG;
    if (!path) throw new Error('Explicit OpenAI org token admission configuration required');
    const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    let policy;
    try {
      const info = await file.stat();
      if (!info.isFile() || info.size > 16384 || info.uid !== process.getuid() || (info.mode & 0o077)) {
        throw new Error('OpenAI token admission configuration must be bounded and owner-only');
      }
      policy = JSON.parse(await file.readFile('utf8'));
    } finally {await file.close();}
    if (policy.schema !== 1 || policy.windowMs !== 60000 || !Array.isArray(policy.models)
      || policy.models.length !== 1 || policy.models[0] !== 'gpt-6.2-astra') {
      throw new Error('Unqualified OpenAI API admission policy');
    }
    admission ??= await SharedTokenAdmission.open(policy);
    pi.registerProvider(withOpenAIAPIAdmission(provider, {admission, models: policy.models}));
  });
  pi.on('session_shutdown', () => {admission?.close(); admission = undefined;});
}
