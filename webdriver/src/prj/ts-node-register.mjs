// `--loader ts-node/esm` is gone in current Node; registering the hook from
// inside the process is the supported spelling.
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';

register('ts-node/esm', pathToFileURL('./'));
