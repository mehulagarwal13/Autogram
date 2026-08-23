import {Config} from '@remotion/cli/config';

// This config file is scoped to the Autogram demo video only (src/) — it does
// not touch the Vite configs under frontend/ or extension/, which are
// separate, unrelated build pipelines.
Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
