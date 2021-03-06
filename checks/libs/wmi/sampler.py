"""
A lightweight Python WMI module wrapper built on top of `pywin32` and `win32com` extensions.

**Specifications**
* Based on top of the `pywin32` and `win32com` third party extensions only
* Compatible with `Raw`* and `Formatted` Performance Data classes
    * Dynamically resolve properties' counter types
    * Hold the previous/current `Raw` samples to compute/format new values*
* Fast and lightweight
    * Avoid queries overhead
    * Cache connections and qualifiers
    * Use `wbemFlagForwardOnly` flag to improve enumeration/memory performance

*\* `Raw` data formatting relies on the avaibility of the corresponding calculator.
Please refer to `checks.lib.wmi.counter_type` for more information*

Original discussion thread: https://github.com/DataDog/dd-agent/issues/1952
Credits to @TheCloudlessSky (https://github.com/TheCloudlessSky)
"""

# stdlib
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from itertools import izip
from utils.timeout import TimeoutException
import pywintypes

# 3p
from win32com.client import Dispatch

# project
from checks.libs.wmi.counter_type import get_calculator, get_raw, UndefinedCalculator


class CaseInsensitiveDict(dict):
    def __setitem__(self, key, value):
        super(CaseInsensitiveDict, self).__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super(CaseInsensitiveDict, self).__getitem__(key.lower())

    def __contains__(self, key):
        return super(CaseInsensitiveDict, self).__contains__(key.lower())

    def get(self, key):
        return super(CaseInsensitiveDict, self).get(key.lower())


class WMISampler(object):
    """
    WMI Sampler.
    """
    # Shared resources
    _wmi_connections = defaultdict(list)

    def __init__(self, logger, class_name, property_names, filters="", host="localhost",
                 namespace="root\\cimv2", username="", password="", and_props=[], timeout_duration=10):
        self.logger = logger

        # Connection information
        self.host = host
        self.namespace = namespace
        self.username = username
        self.password = password

        self.is_raw_perf_class = "_PERFRAWDATA_" in class_name.upper()

        # Sampler settings
        #   WMI class, properties, filters and counter types
        #   Include required properties for making calculations with raw
        #   performance counters:
        #   https://msdn.microsoft.com/en-us/library/aa394299(v=vs.85).aspx
        if self.is_raw_perf_class:
            property_names.extend([
                "Timestamp_Sys100NS",
                "Frequency_Sys100NS",
                # IMPORTANT: To improve performance and since they're currently
                # not needed, do not include the other Timestamp/Frequency
                # properties:
                #   - Timestamp_PerfTime
                #   - Timestamp_Object
                #   - Frequency_PerfTime
                #   - Frequency_Object"
            ])

        self.class_name = class_name
        self.property_names = property_names
        self.filters = filters
        self._and_props = and_props
        self._formatted_filters = None
        self.property_counter_types = None
        self._timeout_duration = timeout_duration

        # FIXME
        # self._query = timeout(timeout_duration)(self._query)

        # Samples
        self.current_sample = None
        self.previous_sample = None

        # Sampling state
        self._sampling = False

    @property
    def connection(self):
        """
        A property to retrieve the sampler connection information.
        """
        return {
            'host': self.host,
            'namespace': self.namespace,
            'username': self.username,
            'password': self.password,
        }

    @property
    def connection_key(self):
        """
        Return an index key used to cache the sampler connection.
        """
        return "{host}:{namespace}:{username}".format(
            host=self.host,
            namespace=self.namespace,
            username=self.username
        )

    @property
    def formatted_filters(self):
        """
        Cache and return filters as a comprehensive WQL clause.
        """
        if not self._formatted_filters:
            filters = deepcopy(self.filters)
            self._formatted_filters = self._format_filter(filters, self._and_props)
        return self._formatted_filters

    def sample(self):
        """
        Compute new samples.
        """
        self._sampling = True

        try:
            if self.is_raw_perf_class and not self.previous_sample:
                self.logger.debug(u"Querying for initial sample for raw performance counter.")
                self.current_sample = self._query()

            self.previous_sample = self.current_sample
            self.current_sample = self._query()
        except TimeoutException:
            self.logger.debug(
                u"Query timeout after {timeout}s".format(
                    timeout=self._timeout_duration
                )
            )
            raise
        else:
            self._sampling = False
            self.logger.debug(u"Sample: {0}".format(self.current_sample))

    def __len__(self):
        """
        Return the number of WMI Objects in the current sample.
        """
        # No data is returned while sampling
        if self._sampling:
            raise TypeError(
                u"Sampling `WMISampler` object has no len()"
            )

        return len(self.current_sample)

    def __iter__(self):
        """
        Iterate on the current sample's WMI Objects and format the property values.
        """
        # No data is returned while sampling
        if self._sampling:
            raise TypeError(
                u"Sampling `WMISampler` object is not iterable"
            )

        if self.is_raw_perf_class:
            # Format required
            for previous_wmi_object, current_wmi_object in \
                    izip(self.previous_sample, self.current_sample):
                formatted_wmi_object = self._format_property_values(
                    previous_wmi_object,
                    current_wmi_object
                )
                yield formatted_wmi_object
        else:
            #  No format required
            for wmi_object in self.current_sample:
                yield wmi_object

    def __getitem__(self, index):
        """
        Get the specified formatted WMI Object from the current sample.
        """
        if self.is_raw_perf_class:
            previous_wmi_object = self.previous_sample[index]
            current_wmi_object = self.current_sample[index]
            formatted_wmi_object = self._format_property_values(
                previous_wmi_object,
                current_wmi_object
            )
            return formatted_wmi_object
        else:
            return self.current_sample[index]

    def __eq__(self, other):
        """
        Equality operator is based on the current sample.
        """
        return self.current_sample == other

    def __str__(self):
        """
        Stringify the current sample's WMI Objects.
        """
        return str(self.current_sample)

    def _get_property_calculator(self, counter_type):
        """
        Return the calculator for the given `counter_type`.
        Fallback with `get_raw`.
        """
        calculator = get_raw
        try:
            calculator = get_calculator(counter_type)
        except UndefinedCalculator:
            self.logger.warning(
                u"Undefined WMI calculator for counter_type {counter_type}."
                " Values are reported as RAW.".format(
                    counter_type=counter_type
                )
            )

        return calculator

    def _format_property_values(self, previous, current):
        """
        Format WMI Object's RAW data based on the previous sample.

        Do not override the original WMI Object !
        """
        formatted_wmi_object = CaseInsensitiveDict()

        for property_name, property_raw_value in current.iteritems():
            counter_type = self.property_counter_types.get(property_name)
            property_formatted_value = property_raw_value

            if counter_type:
                calculator = self._get_property_calculator(counter_type)
                property_formatted_value = calculator(previous, current, property_name)

            formatted_wmi_object[property_name] = property_formatted_value

        return formatted_wmi_object

    @contextmanager
    def get_connection(self):
        """
        Return an existing, available WMI connection or create a new one.
        Release, i.e. mark as available at exit.
        """
        connection = None

        # Fetch an existing connection or create a new one
        available_connections = self._wmi_connections[self.connection_key]

        if available_connections:
            self.logger.debug(
                u"Using cached connection "
                u"(host={host}, namespace={namespace}, username={username}).".format(
                    host=self.host,
                    namespace=self.namespace,
                    username=self.username
                )
            )
            connection = available_connections.pop()
        else:
            self.logger.debug(
                u"Connecting to WMI server "
                u"(host={host}, namespace={namespace}, username={username}).".format(
                    host=self.host,
                    namespace=self.namespace,
                    username=self.username
                )
            )
            locator = Dispatch("WbemScripting.SWbemLocator")
            connection = locator.ConnectServer(
                self.host, self.namespace,
                self.username, self.password
            )

        # Yield the connection
        yield connection

        # Release it
        self._wmi_connections[self.connection_key].append(connection)

    @staticmethod
    def _format_filter(filters, and_props=[]):
        """
        Transform filters to a comprehensive WQL `WHERE` clause.

        Builds filter from a filter list.
        - filters: expects a list of dicts, typically:
                - [{'Property': value},...] or
                - [{'Property': (comparison_op, value)},...]

                NOTE: If we just provide a value we defailt to '=' comparison operator.
                Otherwise, specify the operator in a tuple as above: (comp_op, value)
                If we detect a wildcard character such as '*' or '%' we will override
                the operator to use LIKE
        """
        def build_where_clause(fltr):
            f = fltr.pop()
            wql = ""
            while f:
                prop, value = f.popitem()

                if isinstance(value, tuple):
                    oper = value[0]
                    value = value[1]
                elif isinstance(value, basestring) and '%' in value:
                    oper = 'LIKE'
                else:
                    oper = '='

                if isinstance(value, list):
                    if not len(value):
                        continue

                    internal_filter = map(lambda x:
                                          (prop, x) if isinstance(x, tuple)
                                          else (prop, ('LIKE', x)) if '%' in x
                                          else (prop, (oper, x)), value)

                    bool_op = ' OR '
                    for p in and_props:
                        if p.lower() in prop.lower():
                            bool_op = ' AND '
                            break

                    clause = bool_op.join(['{0} {1} \'{2}\''.format(k, v[0], v[1]) if isinstance(v,tuple)
                                          else '{0} = \'{1}\''.format(k,v)
                                          for k,v in internal_filter])

                    if bool_op.strip() == 'OR':
                        wql += "( {clause} )".format(
                            clause=clause)
                    else:
                        wql += "{clause}".format(
                            clause=clause)

                else:
                    wql += "{property} {cmp} '{constant}'".format(
                        property=prop,
                        cmp=oper,
                        constant=value)
                if f:
                    wql += " AND "

            # empty list skipped
            if wql.endswith(" AND "):
                wql = wql[:-5]

            if len(fltr) == 0:
                return "( {clause} )".format(clause=wql)

            return "( {clause} ) OR {more}".format(
                clause=wql,
                more=build_where_clause(fltr)
            )


        if not filters:
            return ""

        return " WHERE {clause}".format(clause=build_where_clause(filters))

    def _query(self):
        """
        Query WMI using WMI Query Language (WQL) & parse the results.

        Returns: List of WMI objects or `TimeoutException`.
        """
        formated_property_names = ",".join(self.property_names)
        wql = "Select {property_names} from {class_name}{filters}".format(
            property_names=formated_property_names,
            class_name=self.class_name,
            filters=self.formatted_filters,
        )
        self.logger.debug(u"Querying WMI: {0}".format(wql))

        try:
            # From: https://msdn.microsoft.com/en-us/library/aa393866(v=vs.85).aspx
            flag_return_immediately = 0x10  # Default flag.
            flag_forward_only = 0x20
            flag_use_amended_qualifiers = 0x20000

            query_flags = flag_return_immediately | flag_forward_only

            # For the first query, cache the qualifiers to determine each
            # propertie's "CounterType"
            includes_qualifiers = self.is_raw_perf_class and self.property_counter_types is None
            if includes_qualifiers:
                self.property_counter_types = CaseInsensitiveDict()
                query_flags |= flag_use_amended_qualifiers

            with self.get_connection() as connection:
                raw_results = connection.ExecQuery(wql, "WQL", query_flags)

            results = self._parse_results(raw_results, includes_qualifiers=includes_qualifiers)

        except pywintypes.com_error:
            self.logger.warning(u"Failed to execute WMI query (%s)", wql, exc_info=True)
            results = []

        return results

    def _parse_results(self, raw_results, includes_qualifiers):
        """
        Parse WMI query results in a more comprehensive form.

        Returns: List of WMI objects
        ```
        [
            {
                'freemegabytes': 19742.0,
                'name': 'C:',
                'avgdiskbytesperwrite': 1536.0
            }, {
                'freemegabytes': 19742.0,
                'name': 'D:',
                'avgdiskbytesperwrite': 1536.0
            }
        ]
        ```
        """
        results = []
        for res in raw_results:
            # Ensure all properties are available. Use case-insensitivity
            # because some properties are returned with different cases.
            item = CaseInsensitiveDict()
            for prop_name in self.property_names:
                item[prop_name] = None

            for wmi_property in res.Properties_:
                # IMPORTANT: To improve performance, only access the Qualifiers
                # if the "CounterType" hasn't already been cached.
                should_get_qualifier_type = (
                    includes_qualifiers and
                    wmi_property.Name not in self.property_counter_types
                )

                if should_get_qualifier_type:

                    # Can't index into "Qualifiers_" for keys that don't exist
                    # without getting an exception.
                    qualifiers = dict((q.Name, q.Value) for q in wmi_property.Qualifiers_)

                    # Some properties like "Name" and "Timestamp_Sys100NS" do
                    # not have a "CounterType" (since they're not a counter).
                    # Therefore, they're ignored.
                    if "CounterType" in qualifiers:
                        counter_type = qualifiers["CounterType"]
                        self.property_counter_types[wmi_property.Name] = counter_type

                        self.logger.debug(
                            u"Caching property qualifier CounterType: "
                            "{class_name}.{property_names} = {counter_type}"
                            .format(
                                class_name=self.class_name,
                                property_names=wmi_property.Name,
                                counter_type=counter_type,
                            )
                        )
                    else:
                        self.logger.debug(
                            u"CounterType qualifier not found for {class_name}.{property_names}"
                            .format(
                                class_name=self.class_name,
                                property_names=wmi_property.Name,
                            )
                        )

                try:
                    item[wmi_property.Name] = float(wmi_property.Value)
                except (TypeError, ValueError):
                    item[wmi_property.Name] = wmi_property.Value

            results.append(item)
        return results
