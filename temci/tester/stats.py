"""
Statistical helper classes for tested pairs and single blocks.
"""

import copy
import functools
import logging
import traceback
from enum import Enum

import itertools

import math
import path
import sys

from temci.tester.rundata import RunData
from temci.tester.testers import Tester, TesterRegistry
from temci.utils.settings import Settings
import typing as t
import numpy as np
import scipy as sp
import scipy.stats as st
from temci.utils.typecheck import *
import matplotlib.pyplot as plt
import pandas as pd
import matplotlib
from matplotlib2tikz import save as tikz_save
import seaborn as sns

from temci.utils.util import join_strs


class StatMessageType(Enum):

    ERROR = 10
    WARNING = 5

class StatMessageValueFormat(Enum):

    INT = "{}"
    FLOAT = "{:5.5f}"
    PERCENT = "{:5.3%}"

class StatMessage:
    """
    A statistical message that gives a hint to
    """

    message = "{props}: {b_val}"
    hint = ""
    type = None # type: StatMessageType
    border_value = 0
    value_format = StatMessageValueFormat.FLOAT # type: t.Union[StatMessageValueFormat, str]

    def __init__(self, parent: 'BaseStatObject', properties: t.Union[t.List[str], str], values):
        self.parent = parent
        if not isinstance(properties, list):
            properties = [properties]
        if not isinstance(values, list):
            values = [values]
        typecheck(properties, List() // (lambda x: len(x) > 0))
        typecheck(values, List() // (lambda x: len(x) == len(properties)))
        self.properties = sorted(properties)
        self.values = values

    def __add__(self, other: 'StatMessage') -> 'StatMessage':
        typecheck(other, T(type(self)))
        assert self.parent.eq_except_property(other.parent)
        return type(self)(self.parent, self.properties + other.properties, self.values + other.values)

    @staticmethod
    def combine(*messages: t.List[t.Optional['StatMessage']]) -> t.List['StatMessage']:
        """
        Combines all message of the same type and with the same parent in the passed list.
        Ignores None entries.
        :param messages: passed list of messages
        :return: new reduced list
        """
        msgs = set([msg for msg in messages if msg is not None]) # t.Set['StatMessage']
        something_changed = True
        while something_changed:
            something_changed = False
            merged_pair = None # type: t.Tuple['StatMessage', 'StatMessage']
            for (msg, msg2) in itertools.product(msgs, msgs):
                if msg is not msg2:
                    if msg.parent.eq_except_property(msg2.parent) and type(msg) == type(msg2):
                        merged_pair = (msg, msg2)
                        something_changed = True
                        break
            if something_changed:
                msg, msg2 = merged_pair
                msgs.remove(msg)
                msgs.remove(msg2)
                msgs.add(msg + msg2)
        return list(msgs)

    @classmethod
    def _val_to_str(cls, value) -> str:
        format = cls.value_format if isinstance(cls.value_format, str) else cls.value_format.value
        return format.format(value)

    @classmethod
    def check_value(cls, value) -> bool:
        """
        If this fails with the passed value, than the warning is appropriate.
        """
        pass

    @classmethod
    def create_if_valid(cls, parent, value, properties = None, **kwargs) -> t.Union['StatMessage', None]:
        assert isinstance(value, Int()|Float())
        if cls.check_value(value):
            return None
        ret = None
        if properties is not None:
            ret = cls(parent, properties, value, **kwargs)
        else:
            ret = cls(parent, properties, value, **kwargs)
        return ret

    def generate_msg_text(self, show_parent: bool) -> str:
        """
        Generates the text of this message object.
        :param show_parent: Is the parent shown in after the properties? E.g. "blub of bla parent: …"
        :return: message text
        """
        val_strs = list(map(self._val_to_str, self.values))
        prop_strs = ["{} ({})".format(prop, val) for (prop, val) in zip(self.properties, val_strs)]
        props = join_strs(prop_strs)
        if show_parent:
            props += " of {}".format(self.parent)
        return self.message.format(b_val=self._val_to_str(self.border_value), props=props)


class StatWarning(StatMessage):

    type = StatMessageType.WARNING


class StatError(StatWarning, StatMessage):

    type = StatMessageType.ERROR


class StdDeviationToHighWarning(StatWarning):

    message = "The standard deviation per mean of {props} is to high it should be <= {b_val}."
    hint = "With the exec run driver you can probably use the stop_start plugin, preheat and sleep plugins. " \
           "Also consider to increase the number of measured runs."
    border_value = 0.01
    value_format = StatMessageValueFormat.PERCENT

    @classmethod
    def check_value(cls, value) -> bool:
        return value <= cls.border_value


class StdDeviationToHighError(StdDeviationToHighWarning):

    type = StatMessageType.ERROR
    border_value = 0.05


class NotEnoughObservationsWarning(StatWarning):

    message = "The number of observations of {props} is less than {b_val}."
    hint = "Increase the number of measured runs."
    border_value = 30
    value_format = StatMessageValueFormat.INT

    @classmethod
    def check_value(cls, value) -> bool:
        return value >= cls.border_value


class NotEnoughObservationsError(NotEnoughObservationsWarning):

    type = StatMessageType.ERROR
    border_value = 15


class BaseStatObject:
    """
    Class that gives helper methods for the extending stat object classes.
    """

    _filename_counter = 0

    def __init__(self):
        self._stat_messages = []

    def get_stat_messages(self) -> t.List[StatMessage]:
        if not self._stat_messages:
            self._stat_messages = StatMessage.combine(*self._get_stat_messages())
        return self._stat_messages

    def _get_stat_messages(self) -> t.List[StatMessage]:
        raise NotImplementedError()

    def warnings(self) -> t.List[StatMessage]:
        return [x for x in self.get_stat_messages() if x.type is StatMessageType.WARNING]

    def errors(self) -> t.List[StatMessage]:
        return [x for x in self.get_stat_messages() if x.type is StatMessageType.ERROR]

    def has_errors(self) -> bool:
        return any([x.type == StatMessageType.ERROR for x in self.get_stat_messages()])

    def has_warnings(self) -> bool:
        return any([x.type == StatMessageType.WARNING for x in self.get_stat_messages()])

    def get_data_frame(self, **kwargs) -> pd.DataFrame:
        """
        Get the data frame that is associated with this stat object.
        """
        raise NotImplementedError()

    def eq_except_property(self, other) -> bool:
        raise NotImplementedError()

    def _height_for_width(self, width: float) -> float:
        golden_mean = (np.sqrt(5) - 1.0) / 2.0    # Aesthetic ratio
        return width * golden_mean

    def _latexify(self, fig_width: float, fig_height: float = None):
        """Set up matplotlib's RC params for LaTeX plotting.
        Call this before plotting a figure.

        Adapted from http://nipunbatra.github.io/2014/08/latexify/

        Parameters
        ----------
        fig_width : float, optional, inches
        fig_height : float,  optional, inches
        """

        # code adapted from http://www.scipy.org/Cookbook/Matplotlib/LaTeX_Examples

        #MAX_HEIGHT_INCHES = 8.0
        #if fig_height > MAX_HEIGHT_INCHES:
        #    print("WARNING: fig_height too large:" + fig_height +
        #          "so will reduce to" + MAX_HEIGHT_INCHES + "inches.")
        #    fig_height = MAX_HEIGHT_INCHES

        params = {'backend': 'ps',
                  'text.latex.preamble': ['\\usepackage{gensymb}'],
                  'axes.labelsize': 8, # fontsize for x and y labels (was 10)
                  'axes.titlesize': 8,
                  'text.fontsize': 8, # was 10
                  'legend.fontsize': 8, # was 10
                  'xtick.labelsize': 8,
                  'ytick.labelsize': 8,
                  'text.usetex': True,
                  'figure.figsize': list(self._fig_size_cm_to_inch(fig_width,fig_height)),
                  'font.family': 'serif'
        }

        matplotlib.rcParams.update(params)

    def _format_axes(self, ax):
        """
        Adapted from http://nipunbatra.github.io/2014/08/latexify/
        """
        SPINE_COLOR = 'gray'
        for spine in ['top', 'right']:
            ax.spines[spine].set_visible(False)

        for spine in ['left', 'bottom']:
            ax.spines[spine].set_color(SPINE_COLOR)
            ax.spines[spine].set_linewidth(0.5)

        ax.xaxis.set_ticks_position('bottom')
        ax.yaxis.set_ticks_position('left')

        for axis in [ax.xaxis, ax.yaxis]:
            axis.set_tick_params(direction='out', color=SPINE_COLOR)

        return ax

    def _get_new_file_name(self, dir: str) -> str:
        self._filename_counter += 1
        return path.join(path.abspath(dir), str(self._filename_counter))

    def _fig_size_cm_to_inch(self, fig_width: float, fig_height: float) -> t.Tuple[float, float]:
        return fig_width * 0.39370079, fig_height * 0.39370079

    def store_figure(self, dir: str, fig_width: float, fig_height: float = None,
                     pdf: bool = True, tex: bool = True, img: bool = True) -> t.Dict[str, str]:
        """
        Stores the current figure in different formats and returns a dict, that maps
        each used format (pdf, tex or img) to the resulting files name.
        :param dir: base directory that the files are placed into
        :param fig_width: width of the resulting figure (in cm)
        :param fig_height: height of the resulting figure (in cm) or calculated via the golden ratio from fig_width
        :param pdf: store as pdf optimized for publishing
        :param tex: store as tex with pgfplots
        :param img: store as png image
        :return: dictionary mapping each used format to the resulting files name
        """
        if fig_height is None:
            fig_height = self._height_for_width(fig_width)
        filename = self._get_new_file_name(dir)
        ret_dict = {}
        if img:
            ret_dict["img"] = self._store_as_image(filename, fig_width, fig_height)
        if tex:
            ret_dict["tex"] = self._store_as_latex(filename, fig_width, fig_height)
        if pdf:
            ret_dict["pdf"] = self._store_as_pdf(filename, fig_width, fig_height)
        return ret_dict

    def _store_as_pdf(self, filename: str, fig_width: float, fig_height: float) -> str:
        """
        Stores the current figure in a pdf file.
        :warning modifies the current figure
        """
        if not filename.endswith(".pdf"):
            filename += ".pdf"
        rc = copy.deepcopy(matplotlib.rcParams)
        self._latexify(fig_width, fig_height)
        plt.tight_layout()
        self._format_axes(plt.gca())
        plt.savefig(filename)
        matplotlib.rcParams = rc
        return filename

    def _store_as_latex(self, filename: str, fig_width: float, fig_height: float) -> str:
        """
        Stores the current figure as latex in a tex file. Needs pgfplots in latex.
        :see https://github.com/nschloe/matplotlib2tikz
        """
        if not filename.endswith(".tex"):
            filename += ".tex"
        tikz_save(filename, figurewidth="{}cm".format(fig_width), figureheight="{}cm".format(fig_height))
        return filename

    def _store_as_image(self, filename: str, fig_width: float, fig_height: float) -> str:
        """
        Stores the current figure as an png image.
        """
        if not filename.endswith(".png"):
            filename += ".png"
        rc = copy.deepcopy(matplotlib.rcParams)
        matplotlib.rcParams.update['figure.figsize'] = list(self._fig_size_cm_to_inch(fig_width,fig_height))
        plt.savefig(filename)
        matplotlib.rcParams = rc
        return filename

    def _freedman_diaconis_bins(*arrays: t.List) -> int:
        """
        Calculate number of hist bins using Freedman-Diaconis rule.
        If more than one array is passed, the maximum number of bins calculated for each
        array is used.

        Adapted from seaborns source code.
        """
        # From http://stats.stackexchange.com/questions/798/
        def freedman_diaconis(array: np.array):
            h = 2 * sns.utils.iqr(array) / (len(array) ** (1 / 3))
            # fall back to sqrt(a) bins if iqr is 0
            if h == 0:
                return int(np.sqrt(len(array)))
            else:
                return int(np.ceil((max(array) - min(array)) / h))
        return max(map(freedman_diaconis, arrays))

    
    def is_single_valued(self) -> bool:
        """
        Does the data consist only of one unique value?
        """
        raise NotImplementedError()

    def histogram(self, x_ticks: list = None, y_ticks: list = None,
                  show_legend: bool = None, type: str = None,
                  align: str = 'mid', x_label: str = None,
                  y_label: str = None, **kwargs):
        """
        Plots a histogram as the current figure.
        Don't forget to close it via fig.close()
        :param x_ticks: None: use default ticks, list: use the given ticks
        :param y_ticks: None: use default ticks, list: use the given ticks
        :param show_legend: show a legend in the plot? If None only show one if there are more than one sub histograms
        :param type: histogram type (either 'bar', 'barstacked', 'step', 'stepfilled' or None for auto)
        :param align: controls where each bar centered ('left', 'mid' or 'right')
        :param x_label: if not None, shows the given x label
        :param y_lable: if not None: shows the given y label
        :param kwargs: optional arguments passed to the get_data_frame method
        """
        plt.figure()
        if self.is_single_valued():
            logging.error("Can't plot histogram for {} as it's only single valued.".format(self))
            return
        df = self.get_data_frame(**kwargs)
        df_t = df.T
        min_xval = min(map(min, df_t.values))
        max_xval = max(map(max, df_t.values))
        plt.xlim(min_xval, max_xval)
        if type is None:
            type = 'bar' if len(df_t) == 1 else 'step'
        bins = np.linspace(min_xval, max_xval, self._freedman_diaconis_bins(*df_t.values))
        plt.hist(df_t.values, bins=self._freedman_diaconis_bins(*df_t.values),
                 range=(min_xval, max_xval), type=type, align=align,
                 labels=list(df.keys()))
        if x_ticks is not None:
            plt.xticks(x_ticks)
        if y_ticks is not None:
            plt.yticks(y_ticks)
        if show_legend or (show_legend is None and len(df_t) > 1):
            plt.legend()
        if len(df_t) == 1:
            plt.xlabel(df.keys()[0])
        if x_label is not None:
            plt.xlabel(x_label)
        if y_label is not None:
            plt.xlabel(y_label)


class Single(BaseStatObject):
    """
    A statistical wrapper around a single run data object.
    """

    def __init__(self, data: t.Union[RunData, 'Single']):
        super().__init__()
        if isinstance(data, RunData):
            self.rundata = data
        else:
            self.rundata = data.rundata
        self.properties = {} # type: t.Dict[str, SingleProperty]
        """ SingleProperty objects for each property """
        for prop in data.properties:
            self.properties[prop] = SingleProperty(self, self.rundata, prop)

    def _get_stat_messages(self) -> t.List[StatMessage]:
        """
        Combines the messages for all inherited SingleProperty objects (for each property),
        :return: list of all messages
        """
        msgs = [x for prop in self.properties for x in self.properties[prop].get_stat_messages()]
        return msgs

    def get_data_frame(self) -> pd.DataFrame:
        series_dict = {}
        for prop in self.properties:
            series_dict[prop] = pd.Series(self.properties[prop].data, name=prop)
        frame = pd.DataFrame(series_dict, columns=sorted(self.properties.keys()))
        return frame

    def description(self) -> str:
        return self.rundata.description()

    def eq_except_property(self, other) -> bool:
        return isinstance(other, type(self)) and self.rundata == other.rundata

    def __eq__(self, other) -> bool:
        return self.eq_except_property(other)


class SingleProperty(BaseStatObject):
    """
    A statistical wrapper around a single run data block for a specific measured property.
    """

    def __init__(self, parent: Single, data: t.Union[RunData, 'SingleProperty'], property: str):
        super().__init__()
        self.parent = parent
        if isinstance(data, RunData):
            self.rundata = data
            self.data = data[property]
        else:
            self.rundata = data.rundata
            self.data = data.data
        self.array = np.array(self.data)
        self.property = property

    def _get_stat_messages(self) -> t.List[StatMessage]:
        msgs = [
            StdDeviationToHighWarning.create_if_valid(self, self.std_dev_per_mean(), self.property),
            StdDeviationToHighError.create_if_valid(self, self.std_dev_per_mean(), self.property),
            NotEnoughObservationsWarning.create_if_valid(self, self.observations(), self.property),
            NotEnoughObservationsError.create_if_valid(self, self.observations(), self.property)
        ]
        return msgs

    def mean(self) -> float:
        return np.mean(self.array)

    def median(self) -> float:
        return np.median(self.array)

    def min(self) -> float:
        return np.min(self.array)

    def max(self) -> float:
        return np.max(self.array)

    def std_dev(self) -> float:
        """
        Returns the standard deviation.
        """
        return np.std(self.array)

    def std_devs(self) -> t.Tuple[float, float]:
        """
        Calculates the standard deviation of elements <= mean and of the elements > mean.
        :return: (lower, upper)
        """
        mean = self.mean()

        def std_dev(elements: list) -> float:
            return np.sqrt(sum(np.power(x - mean, 2) for x in elements) / (len(elements) - 1))

        lower = [x for x in self.array if x <= mean]
        upper = [x for x in self.array if x > mean]
        return std_dev(lower), std_dev(upper)

    def std_dev_per_mean(self) -> float:
        return self.std_dev() / self.mean()
    
    def variance(self) -> float:
        return np.var(self.array)
    
    def observations(self) -> int:
        return len(self.data)
    
    def __len__(self) -> int:
        return len(self.data)

    def eq_except_property(self, other) -> bool:
        return isinstance(other, SingleProperty) and self.rundata == other.rundata

    def __eq__(self, other):
        return self.eq_except_property(other) and self.property == other.property
    
    def sem(self) -> float:
        """
        Returns the standard error of the mean (standard deviation / sqrt(observations)).
        """
        return st.sem(self.array)
    
    def std_error_mean(self) -> float:
        return st.sem(self.array)

    def mean_ci(self, alpha: float) -> t.Tuple[float, float]:
        """
        Calculates the confidence interval in which the population mean lies with the given probability.
        Assumes normal distribution.
        :param alpha: given probability
        :return: lower, upper bound
        :see http://stackoverflow.com/a/15034143
        """
        h = self.std_error_mean() * st.t._ppf((1+alpha)/2.0, self.observations() - 1)
        return self.mean() - h, self.mean() + h

    def std_dev_ci(self, alpha: float) -> t.Tuple[float, float]:
        """
        Calculates the confidence interval in which the standard deviation lies with the given probability.
        Assumes normal distribution.
        :param alpha: given probability
        :return: lower, upper bound
        :see http://www.stat.purdue.edu/~tlzhang/stat511/chapter7_4.pdf
        """
        var = self.variance() * (self.observations() - 1)
        upper = np.sqrt(var / st.t._ppf(alpha/2.0, self.observations() - 1))
        lower = np.sqrt(var / st.t._ppf(1-alpha/2.0, self.observations() - 1))
        return lower, upper

    def is_single_valued(self) -> bool:
        """
        Does the data consist only of one unique value?
        """
        return len(set(self.data)) == 1

    def __str__(self) -> str:
        return self.rundata.description()

    def get_data_frame(self) -> pd.DataFrame:
        series_dict = {self.property: pd.Series(self.data, name=self.property)}
        frame = pd.DataFrame(series_dict, columns=[self.property])
        return frame


class TestedPair(BaseStatObject):
    """
    A statistical wrapper around two run data objects that are compared via a tester.
    """

    def __init__(self, first: t.Union[RunData, Single], second: t.Union[RunData, Single], tester: Tester = None):
        super().__init__()
        self.first = Single(first)
        self.second = Single(second)
        self.tester = tester or TesterRegistry.get_for_name(TesterRegistry.get_used(),
                                                            Settings()["stats/tester"],
                                                            Settings()["stats/uncertainty_range"])
        self.properties = {} # type: t.Dict[str, TestedPairProperty]
        """ TestedPairProperty objects for each shared property of the inherited Single objects """
        for prop in set(self.first.properties.keys()).intersection(self.second.properties.keys()):
            self.properties[prop] = TestedPairProperty(self, self.first, self.second, prop, tester)
    
    def _get_stat_messages(self) -> t.List[StatMessage]:
        """
        Combines the messages for all inherited TestedPairProperty objects (for each property),
        :return: simplified list of all messages
        """
        msgs = [x for prop in self.properties for x in self.properties[prop].get_stat_messages()]
        return msgs

    def rel_difference(self) -> float:
        """
        Calculates the geometric mean of the relative mean differences (first - second) / first.
        :see http://www.cse.unsw.edu.au/~cs9242/15/papers/Fleming_Wallace_86.pdf
        """
        mean = sum(x.mean_diff_per_mean() for x in self.properties.values())
        if mean == 0:
            return 1
        sig = np.sign(mean)
        return sig * math.pow(abs(mean), 1 / len(self.properties))

    def swap(self) -> 'TestedPair':
        """
        Creates a new pair with the elements swapped.
        :return: new pair object
        """
        return TestedPair(self.second, self.first, self.tester)

    def __getitem__(self, property: str) -> 'TestedPairProperty':
        return self.properties[property]

    def eq_except_property(self, other) -> bool:
        return isinstance(other, type(self)) and self.first == other.first and self.second == other.second \
               and self.tester == other.tester

    def __eq__(self, other) -> bool:
        return self.eq_except_property(other)

class TestedPairsAndSingles(BaseStatObject):
    """
    A wrapper around a list of tested pairs and singles.
    """

    def __init__(self, singles: t.List[t.Union[RunData, Single]], pairs: t.List[TestedPair] = None):
        super().__init__()
        self.singles = list(map(Single, singles)) # type: t.List[Single]
        self.pairs = pairs or [] # type: t.List[TestedPair]
        if pairs is None and len(self.singles) > 1:
            for i in range(0, len(self.singles) - 1):
                for j in range(i + 1, len(self.singles)):
                    self.pairs.append(self.get_pair(i, j))

    def number_of_singles(self) -> int:
        return len(self.singles)

    def get_pair(self, first_id: int, second_id: int) -> TestedPair:
        l = self.number_of_singles()
        assert 0 <= first_id < l and 0 <= second_id < l
        return TestedPair(self.singles[first_id], self.singles[second_id])

    def properties(self) -> t.List[str]:
        """
        Returns the properties that are shared among all single run data objects.
        """
        if self.singles == []:
            return
        props = set(self.singles[0].properties.keys())
        for single in self.singles[1:]:
            props.intersection_update(single.properties.keys())
        return sorted(props)

    def get_stat_messages(self) -> t.List[StatMessage]:
        """
        Combines the messages for all inherited TestedPair and Single objects,
        :return: simplified list of all messages
        """
        msgs = []
        for pair in self.pairs:
            msgs.extend(pair.get_stat_messages())
        return msgs

    def __getitem__(self, id: int) -> Single:
        assert 0 <= id < self.number_of_singles()
        return self.singles[id]


class EffectToSmallWarning(StatWarning):

    message = "The mean difference per standard deviation of {props} is less than {b_val}."
    hint = "Try to reduce the standard deviation if you think that the measured difference is significant: " \
           "With the exec run driver you can probably use the stop_start plugin, preheat and sleep plugins. " \
           "Also consider increasing the number of measured runs."
    border_value = 2
    value_format = StatMessageValueFormat.FLOAT

    @classmethod
    def check_value(cls, value) -> bool:
        return value >= cls.border_value


class EffectToSmallError(EffectToSmallWarning):

    type = StatMessageType.ERROR
    border_value = 1


class TestedPairProperty(BaseStatObject):
    """
    Statistic helper for a compared pair of run data blocks for a specific measured property.
    """

    def __init__(self, parent: TestedPair, first: Single, second: Single, property: str, tester: Tester = None):
        super().__init__()
        self.parent = parent
        self.first = SingleProperty(first, first.rundata, property)
        self.second = SingleProperty(first, second.rundata, property)
        self.tester = tester or TesterRegistry.get_for_name(TesterRegistry.get_used(),
                                                            Settings()["stats/tester"],
                                                            Settings()["stats/uncertainty_range"])
        self.property = property
    
    def _get_stat_messages(self) -> t.List[StatMessage]:
        """
        Combines the messages for all inherited TestedPairProperty objects (for each property),
        :return: simplified list of all messages
        """
        msgs = self.first.get_stat_messages() + self.second.get_stat_messages()
        msgs += [
            EffectToSmallWarning.create_if_valid(self, self.mean_diff_per_dev(), self.property),
            EffectToSmallError.create_if_valid(self, self.mean_diff_per_dev(), self.property)
        ]
        return msgs
    
    def mean_diff(self) -> float:
        return self.first.mean() - self.second.mean()

    def mean_diff_ci(self, alpha: float) -> t.Tuple[float, float]:
        """
        Calculates the confidence interval in which the mean difference lies with the given probability.
        Assumes normal distribution.
        :param alpha: given probability
        :return: lower, upper bound
        :see http://www.kean.edu/~fosborne/bstat/06b2means.html
        """
        d = self.mean_diff()
        t =  st.t.ppf(1-alpha/2.0) * np.sqrt(self.first.variance() / self.first.observations() -
                                             self.second.variance() / self.second.observations())
        return d - t, d + t
    
    def mean_diff_per_mean(self) -> float:
        """
        :return: (mean(A) - mean(B)) / mean(A)
        """
        return self.mean_diff() / self.first.mean()
    
    def mean_diff_per_dev(self) -> float:
        """
        Calculates the mean difference per standard deviation (maximum of first and second).
        """
        return self.mean_diff() / self.max_std_dev()
    
    def equal_prob(self) -> float:
        """
        Probability of the nullhypothesis being not not correct (three way logic!!!).
        :return: p value between 0 and 1
        """
        return self.tester.test(self.first.data, self.second.data)
    
    def is_equal(self) -> t.Union[None, bool]:
        """
        Checks the nullhypthosesis.
        :return: True or False if the p val isn't in the uncertainty range of the tester, None else
        """
        if self.tester.is_uncertain(self.first.data, self.second.data):
            return None
        return self.tester.is_equal(self.first.data, self.second.data)

    def mean_std_dev(self) -> float:
        return (self.first.mean() + self.second.mean()) / 2

    def max_std_dev(self) -> float:
        return max(self.first.mean(), self.second.mean())

    def get_data_frame(self, show_property = True) -> pd.DataFrame:
        columns = []
        if show_property:
            columns = ["{}: {}".format(self.first, self.property),
                             "{}: {}".format(self.second, self.property)]
        else:
            columns = [str(self.first), str(self.second)]
        series_dict = {
            columns[0]: pd.Series(self.first.data, name=columns[0]),
            columns[1]: pd.Series(self.first.data, name=columns[1])
        }
        frame = pd.DataFrame(series_dict, columns=columns)
        return frame

    def is_single_valued(self) -> bool:
        return self.first.is_single_valued() and self.second.is_single_valued()

    def eq_except_property(self, other) -> bool:
        return isinstance(other, type(self)) and self.first.eq_except_property(self.second) \
               and self.tester == other.tester

    def __eq__(self, other):
        return self.eq_except_property(other) and self.property == other.property