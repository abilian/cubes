# -*- coding: utf-8 -*-

import os.path

from typing import List, Dict, Any, Optional, Tuple, Union, Type
from logging import Logger

from collections import OrderedDict, defaultdict
from configparser import ConfigParser

from .metadata import (
    read_model_metadata,
    find_dimension,
    LocalizationContext,
    Cube,
    Dimension,
)
from .metadata.providers import ModelProvider
from .auth import NotAuthorized, Authorizer
from .common import read_json_file
from .errors import ConfigurationError, ArgumentError, CubesError
from .logging import get_logger
from .calendar import Calendar
from .namespace import Namespace
from .stores import Store
from .query.browser import AggregationBrowser, BrowserFeatures
from .types import _CubeKey, JSONType
from . import ext
from .settings import Setting, SettingType, distill_settings

# FIXME: [typing] Remove direct reference to SQL, move to shared place
from .sql.mapper import NamingDict, distill_naming

__all__ = ["Workspace"]


SLICER_INFO_KEYS = (
    "name",
    "label",
    "description",  # Workspace model description
    "copyright",  # Copyright for the data
    "license",  # Data license
    "maintainer",  # Name (and maybe contact) of data maintainer
    "contributors",  # List of contributors
    "visualizers",  # List of dicts with url and label of server's visualizers
    "keywords",  # List of keywords describing server's cubes
    "related",  # List of dicts with related servers
)

WORKSPACE_SETTINGS = [
    Setting("log", SettingType.str, desc="File name where the logs are written"),
    Setting(
        "log_level",
        SettingType.str,
        desc="Log level details",
        values=["info", "error", "warn", "debug"],
    ),
    Setting("root_directory", SettingType.str, desc="Directory for all relative paths"),
    Setting(
        "models_directory",
        SettingType.str,
        desc="Place where file-based models are searched for",
    ),
    Setting(
        "info_file", SettingType.str, desc="A JSON file where server info is stored"
    ),
    Setting(
        "stores_file",
        SettingType.str,
        desc="Configuration file with configuration of stores",
    ),
    Setting(
        "timezone", SettingType.str, desc="Default timezone for time and date functions"
    ),
    Setting(
        "first_weekday",
        SettingType.str,
        desc="Name or a number of a first day of the week",
    ),
]


class Workspace:

    # TODO: Make this first-class object
    store_infos: Dict[str, Tuple[str, JSONType]]
    stores: Dict[str, Store]
    logger: Logger
    root_dir: str
    models_dir: str
    namespace: Namespace
    calendar: Calendar

    info: JSONType
    browser_options: JSONType
    # TODO: Review use of this. Is this still needed? Can it be moved?
    options: JSONType
    authorizer: Optional[Authorizer]
    # FIXME: [typing] Fix the value type to NamingType or SettingValue
    namings: Dict[str, NamingDict]

    ns_languages: JSONType

    _cubes: Dict[_CubeKey, Cube]

    def __init__(
        self,
        config: ConfigParser = None,
        stores: str = None,
        load_base_model: bool = True,
        **_options: Any,
    ) -> None:
        """Creates a workspace. `config` should be a `ConfigParser` or a
        path to a config file. `stores` should be a dictionary of store
        configurations, a `ConfigParser` or a path to a ``stores.ini`` file.

        Properties:

        * `stores` – dictionary of stores
        * `store_infos` – dictionary of store configurations
        * `namespace` – default namespace
        * `logger` – workspace logegr
        * `rot_dir` – root directory where all relative paths are looked for
        * `models_dir` – directory with models (if relative, then relative to
          the root directory)

        * `info` – info dictionary from the info file or info section
        * `calendar` – calendar object providing date and time functions
        * `ns_languages` – dictionary where keys are namespaces and values
          are language to translation path mappings.
        """

        timezone: Optional[str]
        first_weekday: Union[str, int]
        options: JSONType
        info: JSONType

        # FIXME: **_options is temporary solution/workaround before we get
        # better configuration. Used internally. Don't use!

        if not config:
            config = ConfigParser()

        self.store_infos = {}
        self.stores = {}

        # Logging
        # =======
        # Log to file or console
        if "workspace" in config:
            workspace_config = distill_settings(
                config["workspace"], WORKSPACE_SETTINGS, owner="workspace"
            )
        else:
            workspace_config = {}

        if "log" in workspace_config:
            self.logger = get_logger(path=workspace_config["log"])
        else:
            self.logger = get_logger()

        # Change to log level if necessary
        if "log_level" in workspace_config:
            self.logger.setLevel(workspace_config["log_level"].upper())

        # Set the default models path
        if "root_directory" in workspace_config:
            self.root_dir = workspace_config["root_directory"]
        elif "cubes_root" in _options:
            # FIXME: this is quick workaround, see note at the beginning of
            # this method
            self.root_dir = _options["cubes_root"]
        else:
            self.root_dir = "."

        # FIXME: Pick only one
        if "models_directory" in workspace_config:
            self.models_dir = workspace_config["models_directory"]
        else:
            self.models_dir = "."

        if self.root_dir and not os.path.isabs(self.models_dir):
            self.models_dir = os.path.join(self.root_dir, self.models_dir)

        if self.models_dir:
            self.logger.debug(f"Models root: {self.models_dir}")
        else:
            self.logger.debug("Models root set to current directory")

        # Namespaces and Model Objects
        # ============================

        self.namespace = Namespace()

        # Cache of created global objects
        self._cubes = {}
        # Note: providers are responsible for their own caching

        # Info
        # ====

        self.info = OrderedDict()

        # TODO: [2.0] Move to server
        if "info_file" in workspace_config:
            path = workspace_config["info_file"]

            if self.root_dir and not os.path.isabs(path):
                path = os.path.join(self.root_dir, path)

            info = read_json_file(path, "Slicer info")
            for key in SLICER_INFO_KEYS:
                self.info[key] = info.get(key)

        elif "info" in config:
            info = dict(config["info"])

            if "visualizer" in info:
                info["visualizers"] = [
                    {
                        "label": info.get("label", info.get("name", "Default")),
                        "url": info["visualizer"],
                    }
                ]

            for key in SLICER_INFO_KEYS:
                self.info[key] = info.get(key)

        # Register stores from external stores.ini file or a dictionary
        if not stores and "stores_file" in workspace_config:
            stores = workspace_config["stores_file"]

            # Prepend the root directory if stores is relative
            if self.root_dir and not os.path.isabs(stores):
                stores = os.path.join(self.root_dir, stores)

        # TODO: Don't accept both, only one
        if isinstance(stores, str):
            store_config = ConfigParser()
            try:
                store_config.read(stores)
            except Exception as e:
                raise ConfigurationError(
                    f"Unable to read stores from {stores}. Reason: {e}"
                )

            for store in store_config.sections():
                self._register_store_dict(store, dict(store_config.items(store)))

        elif isinstance(stores, dict):
            for name, store in stores.items():
                self._register_store_dict(name, store)

        elif stores is not None:
            raise ConfigurationError(
                "Unknown stores description object: %s" % (type(stores))
            )

        # Calendar
        # ========

        timezone = workspace_config.get("timezone")
        first_weekday = workspace_config.get("first_weekday", 0)

        self.logger.debug(
            f"Workspace calendar timezone: {timezone} "
            "first week day: {first_weekday}"
        )

        self.calendar = Calendar(timezone=timezone, first_weekday=first_weekday)

        # Register Naming
        #

        # TODO: This is temporary - just one naming convention. We need to be a
        # ble to specify more.

        namigs = Dict[str, NamingDict]
        self.namings = {}
        if "naming" in config:
            self.namings["default"] = distill_naming(config["naming"])

        # Register Stores
        # ===============
        #
        # * Default store is [store] in main config file
        # * Stores are also loaded from main config file from sections with
        #   name [store_*] (not documented feature)

        # TODO: Convert to Options
        default_store_info: Optional[JSONType] = None
        if "store" in config:
            default_store_info = dict(config["store"])

        if default_store_info:
            self._register_store_dict("default", default_store_info)

        # Register [store_*] from main config (not documented)
        for section in config.sections():
            if section != "store" and section.startswith("store"):
                name = section[6:]
                self._register_store_dict(name, config[section])

        if "browser" in config:
            self.browser_options = dict(config["browser"])
        else:
            self.browser_options = {}

        if "main" in config:
            self.options = dict(config["main"])
        else:
            self.options = {}

        # Register Languages
        # ==================
        #

        # Register [language *]
        self.ns_languages = defaultdict(dict)
        for section in config.sections():
            if section.startswith("locale"):
                lang = section[9:]
                # namespace -> path
                for nsname, path in config[section].items():
                    if nsname == "defalt":
                        ns = self.namespace
                    else:
                        (ns, _) = self.namespace.namespace(nsname)
                    ns.add_translation(lang, path)

        # Authorizer
        # ==========

        if "authorization" in workspace_config:
            auth_type = workspace_config["authorization"]
            # TODO: Use Options
            auth_options = dict(config["authorization"])
            auth_options["cubes_root"] = self.root_dir
            self.authorizer = ext.authorizer(auth_type, **auth_options)
        else:
            self.authorizer = None

        # Configure and load models
        # =========================

        # Models are searched in:
        # [model]
        # [models] * <- depreciated!
        # TODO: add this for nicer zero-conf
        # root/model.json
        # root/main.cubesmodel
        # models/*.cubesmodel
        models: List[Tuple[str, str]]
        models = []
        # Undepreciated
        if "model" in config:
            if "path" not in config["model"]:
                raise ConfigurationError("No model path specified")

            path = config["model"]["path"]
            models.append(("main", path))

        # TODO: Depreciate this too
        if "models" in config:
            models += config["models"].items()

        for model_name, path in models:
            self.logger.debug(f"Loading model {model_name}")
            self.import_model(path)

    def flush_lookup_cache(self) -> None:
        """Flushes the cube lookup cache."""
        self._cubes.clear()
        # TODO: flush also dimensions

    def _get_namespace(self, ref: str) -> Namespace:
        """Returns namespace with ference `ref`"""
        if not ref or ref == "default":
            return self.namespace
        return self.namespace.namespace(ref)[0]

    def add_translation(
        self, locale: str, trans: JSONType, ns: str = "default"
    ) -> None:
        """Add translation `trans` for `locale`. `ns` is a namespace. If no
        namespace is specified, then default (global) is used."""

        namespace = self._get_namespace(ns)
        namespace.add_translation(locale, trans)

    # TODO: Make `info` dict of Options
    def _register_store_dict(self, name: str, info: JSONType) -> None:
        info = dict(info)
        try:
            type_ = info.pop("type")
        except KeyError:
            try:
                type_ = info.pop("backend")
            except KeyError:
                raise ConfigurationError("Store '%s' has no type specified" % name)
            else:
                self.logger.warn(
                    "'backend' is depreciated, use 'type' for "
                    "store (in %s)." % str(name)
                )

        self.register_store(name, type_, **info)

    # TODO: Make `config` use Options
    def register_default_store(self, type_: str, **config: Any) -> None:
        """Convenience function for registering the default store. For more
        information see `register_store()`"""
        self.register_store("default", type_, **config)

    # TODO: Make `config` use Options
    def register_store(
        self, name: str, type_: str, include_model: bool = True, **_config: Any
    ) -> None:
        """Adds a store configuration."""

        config = dict(_config)

        if name in self.store_infos:
            raise ConfigurationError("Store %s already registered" % name)

        self.store_infos[name] = (type_, config)

        # Model and provider
        # ------------------

        # If store brings a model, then include it...
        if include_model and "model" in config:
            model = config.pop("model")
        else:
            model = None

        # Get related model provider or override it with configuration
        store_factory = Store.concrete_extension(type_)

        if hasattr(store_factory, "related_model_provider"):
            provider = store_factory.related_model_provider
        else:
            provider = None

        provider = config.pop("model_provider", provider)

        nsname = config.pop("namespace", None)

        if model:
            self.import_model(model, store=name, namespace=nsname, provider=provider)
        elif provider:
            # Import empty model and register the provider
            self.import_model({}, store=name, namespace=nsname, provider=provider)

        self.logger.debug("Registered store '%s'" % name)

    # TODO: Rename to _model_store_name
    def _store_for_model(self, metadata: JSONType) -> str:
        """Returns a store for model specified in `metadata`. """
        store_name = metadata.get("store")
        if not store_name and "info" in metadata:
            store_name = metadata["info"].get("store")

        store_name = store_name or "default"

        return store_name

    # TODO: this is very confusing process, needs simplification
    # TODO: change this to: add_model_provider(provider, info, store, languages, ns)
    def import_model(
        self,
        model: Union[JSONType, str] = None,
        provider: Union[str, ModelProvider] = None,
        store: str = None,
        translations: JSONType = None,
        namespace: str = None,
    ) -> None:
        """Registers the `model` in the workspace. `model` can be a
        metadata dictionary, filename, path to a model bundle directory or a
        URL.

        If `namespace` is specified, then the model's objects are stored in
        the namespace of that name.

        `store` is an optional name of data store associated with the model.
        If not specified, then the one from the metadata dictionary will be
        used.

        Model's provider is registered together with loaded metadata. By
        default the objects are registered in default global namespace.

        Note: No actual cubes or dimensions are created at the time of calling
        this method. The creation is deferred until
        :meth:`cubes.Workspace.cube` or :meth:`cubes.Workspace.dimension` is
        called.
        """
        # 1. Metadata
        # -----------
        # Make sure that the metadata is a dictionary
        #
        # TODO: Use "InlineModelProvider" and "FileBasedModelProvider"

        # 1. Model Metadata
        # -----------------
        # Make sure that the metadata is a dictionary
        #
        # TODO: Use "InlineModelProvider" and "FileBasedModelProvider"

        if isinstance(model, str):
            self.logger.debug(
                f"Importing model from {model}. "
                f"Provider: {provider} Store: {store} "
                f"NS: {namespace}"
            )
            path = model
            if self.models_dir and not os.path.isabs(path):
                path = os.path.join(self.models_dir, path)
            model = read_model_metadata(path)

        elif isinstance(model, dict):
            self.logger.debug(
                f"Importing model from dictionary. "
                f"Provider: {provider} Store: {store} "
                f"NS: {namespace}"
            )
        elif model is None:
            model = {}
        else:
            raise ConfigurationError(
                f"Unknown model '{model}' " f"(should be a filename or a dictionary)"
            )

        # 2. Model provider
        # -----------------
        # Create a model provider if name is given. Otherwise assume that the
        # `provider` is a ModelProvider subclass instance

        provider_obj: Optional[ModelProvider] = None

        if isinstance(provider, str):
            provider_name = provider
            provider_obj = ModelProvider.concrete_extension(provider)(model)
        else:
            provider_obj = provider

        # TODO: remove this, if provider is external, it should be specified
        if not provider_obj:
            provider_name = model.get("provider", "static")
            provider_obj = ModelProvider.concrete_extension(provider_name)(model)

        # 3. Store
        # --------
        # Link the model with store
        store = store or model.get("store")

        if store or (
            hasattr(provider_obj, "requires_store") and provider_obj.requires_store()
        ):
            provider_obj.bind(self.get_store(store))

        # 4. Namespace
        # ------------

        if namespace:
            if namespace == "default":
                ns = self.namespace
            elif isinstance(namespace, str):
                (ns, _) = self.namespace.namespace(namespace, create=True)
            else:
                ns = namespace
        elif store == "default":
            ns = self.namespace
        else:
            # Namespace with the same name as the store.
            (ns, _) = self.namespace.namespace(store, create=True)

        ns.add_provider(provider_obj)

    # TODO: Change to Options type
    def add_slicer(self, name: str, url: str, **options: Any) -> None:
        """Register a slicer as a model and data provider."""
        self.register_store(name, "slicer", url=url, **options)
        self.import_model({}, provider="slicer", store=name)

    def cube_names(self, identity: Any = None) -> List[str]:
        """Return names all available cubes."""
        return [cube["name"] for cube in self.list_cubes()]

    # TODO: this is not loclized!!!
    # TODO: Convert this to CubeDescriptions
    def list_cubes(self, identity: Any = None) -> List[Dict[str, str]]:
        """Get a list of metadata for cubes in the workspace. Result is a list
        of dictionaries with keys: `name`, `label`, `category`, `info`.

        The list is fetched from the model providers on the call of this
        method.

        If the workspace has an authorizer, then it is used to authorize the
        cubes for `identity` and only authorized list of cubes is returned.
        """

        all_cubes = self.namespace.list_cubes(recursive=True)

        if self.authorizer:
            by_name = {cube["name"]: cube for cube in all_cubes}
            names = [cube["name"] for cube in all_cubes]

            authorized = self.authorizer.authorize(identity, names)
            all_cubes = [by_name[name] for name in authorized]

        return all_cubes

    def cube(self, ref: str, identity: Any = None, locale: str = None) -> Cube:
        """Returns a cube with full cube namespace reference `ref` for user
        `identity` and translated to `locale`."""

        if not isinstance(ref, str):
            raise TypeError("Reference is not a string, is %s" % type(ref))

        if self.authorizer:
            authorized = self.authorizer.authorize(identity, [ref])
            if not authorized:
                raise NotAuthorized

        # If we have a cached cube, return it
        # See also: flush lookup
        cube_key: _CubeKey = (ref, identity, locale)
        if cube_key in self._cubes:
            return self._cubes[cube_key]

        # Find the namespace containing the cube – we will need it for linking
        # later
        (namespace, provider, basename) = self.namespace.find_cube(ref)

        cube = provider.cube(basename, locale=locale, namespace=namespace)
        cube.namespace = namespace
        cube.store = provider.store

        # TODO: cube.ref -> should be ref and cube.name should be basename
        cube.basename = basename
        cube.name = ref

        lookup = namespace.translation_lookup(locale)

        if lookup:
            # TODO: pass lookup instead of jsut first found translation
            context = LocalizationContext(lookup[0])
            trans = context.object_localization("cubes", cube.name)
            cube = cube.localized(trans)

        # Cache the cube
        self._cubes[cube_key] = cube

        return cube

    def dimension(
        self, name: str, locale: str = None, namespace: str = None, provider: str = None
    ) -> Dimension:
        """Returns a dimension with `name`. Raises `NoSuchDimensionError` when
        no model published the dimension. Raises `RequiresTemplate` error when
        model provider requires a template to be able to provide the
        dimension, but such template is not a public dimension.

        The standard lookup when linking a cube is:

        1. look in the cube's provider
        2. look in the cube's namespace – all providers within that namespace
        3. look in the default (global) namespace
        """

        return find_dimension(name, locale, namespace or self.namespace, provider)

    def _browser_options(self, cube: Cube) -> JSONType:
        """Returns browser configuration options for `cube`. The options are
        taken from the configuration file and then overriden by cube's
        `browser_options` attribute."""

        options = dict(self.browser_options)
        if cube.browser_options:
            options.update(cube.browser_options)

        return options

    def browser(
        self, cube: Cube, locale: str = None, identity: Any = None
    ) -> AggregationBrowser:
        """Returns a browser for `cube`."""

        naming: NamingDict

        # TODO: bring back the localization
        # model = self.localized_model(locale)

        if isinstance(cube, str):
            cube = self.cube(cube, identity=identity)

        # We don't allow cube store to be an actual store. Cube is a logical
        # object.
        assert (
            isinstance(cube.store, str) or cube.store is None
        ), f"Store of a cube ({cube}) must be a string or None"

        locale = locale or cube.locale

        store_name = cube.store or "default"
        store = self.get_store(store_name)
        store_type = self.store_infos[store_name][0]
        store_info = self.store_infos[store_name][1]

        # TODO: Review necessity of this
        store_type = store.extension_name
        assert store_type is not None, f"Store type should not be None ({store})"

        cube_options = self._browser_options(cube)

        # TODO: merge only keys that are relevant to the browser!
        options = dict(store_info)
        options.update(cube_options)

        # TODO: Construct options for the browser from cube's options
        # dictionary and workspece default configuration

        browser_name = cube.browser
        if not browser_name and hasattr(store, "default_browser_name"):
            browser_name = store.default_browser_name
        if not browser_name:
            browser_name = store_type
        if not browser_name:
            raise ConfigurationError("No store specified for cube '%s'" % cube)

        cls: Type[AggregationBrowser]
        cls = AggregationBrowser.concrete_extension(browser_name)
        # FIXME: [2.0] This should go away with query redesign
        options.pop("url", None)

        naming_name: str
        naming_name = options.pop("naming", "default")
        if naming_name in self.namings:
            naming = distill_naming(self.namings[naming_name])
        else:
            naming = None

        settings = cls.distill_settings(options)

        # FIXME: [typing] Not correct type-wise
        browser = cls(
            cube=cube,
            store=store,
            locale=locale,
            calendar=self.calendar,
            naming=naming,
            **settings,
        )

        # TODO: remove this once calendar is used in all backends
        browser.calendar = self.calendar

        return browser

    def cube_features(self, cube: Cube, identity: Any = None) -> BrowserFeatures:
        """Returns browser features for `cube`"""
        # TODO: this might be expensive, make it a bit cheaper
        # recycle the feature-providing browser or something. Maybe use class
        # method for that
        return self.browser(cube, identity).features()

    def get_store(self, name: str = None) -> Store:
        """Opens a store `name`. If the store is already open, returns the
        existing store."""

        name = name or "default"

        if name in self.stores:
            return self.stores[name]

        try:
            type_, options = self.store_infos[name]
        except KeyError:
            raise ConfigurationError(f"Unknown store '{name}'")

        # TODO: temporary hack to pass store name and store type
        ext: Store
        ext = Store.concrete_extension(type_)
        store = ext.create_with_dict(options)
        # FIXME: Clean-up
        # store = Store.concrete_extension(type_)(store_type=type_, **options)
        self.stores[name] = store
        return store
