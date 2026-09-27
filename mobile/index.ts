import { registerRootComponent } from "expo";
import type { ComponentType } from "react";

import App from "./src/App";

// Every prop on App is optional - they exist so the tests can inject a fake push
// registration and URL opener. Expo's root-component type does not model an
// all-optional props object, hence the cast.
registerRootComponent(App as ComponentType);
