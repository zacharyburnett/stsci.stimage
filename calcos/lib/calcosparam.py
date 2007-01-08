# This file defines parameters used by calcos.

CALCOS_VERSION_NUMBER = "1.1"
CALCOS_VERSION_DATE = "(8 January 2007)"
CALCOS_VERSION = CALCOS_VERSION_NUMBER + " " + CALCOS_VERSION_DATE

SPEED_OF_LIGHT = 299792.458     # km/s

DAYS_PER_YEAR = 365.25
SEC_PER_DAY = 86400.

MJD_TO_JD = 2400000.5           # add to MJD to get Julian Day Number

# Live time estimates should not differ by more than this fraction of
# the live time.
LIVETIME_CRITERION = 0.1

# This is the wavelength below which no significant flux could be detected.
MIN_WAVELENGTH = 900.           # Angstroms

# These give the axis lengths of the FUV and NUV detectors, in pixels.
FUV_X = 16384                   # more rapidly varying axis
FUV_Y = 1024
NUV_X = 1024                    # more rapidly varying axis
NUV_Y = 1024

# These give the number of spectra per detector (used in extract.py).
FUV_SPECTRA = 1                 # one spectrum on one FUV segment
NUV_SPECTRA = 3                 # three stripes on NUV detector

# These are the possible values for verbosity.
QUIET = 0
VERBOSE = 1
VERY_VERBOSE = 2

# These are the possible values for the TAGFLASH keyword, and corresponding
# integer codes.
TAGFLASH_NONE = "NONE"
TAGFLASH_AUTO = "AUTO"
TAGFLASH_UNIFORMLY_SPACED = "UNIFORMLY SPACED"
TAGFLASH_TYPE_NONE = 0
TAGFLASH_TYPE_AUTO = 1
TAGFLASH_TYPE_UNIFORMLY_SPACED = 2

# The following three parameters are used by getTable.
# NOT_APPLICABLE will be assigned as the value of a keyword that is
# missing from the header; this is done because some keywords may
# actually not be present, while others that are not relevant will be
# present but have the value "N/A".
STRING_WILDCARD = "ANY"
NOT_APPLICABLE = "N/A"
INT_WILDCARD = -1

# These are the data quality flags.
DQ_OK = 0                       # no anomalous condition noted
DQ_SOFTERR = 1                  # Reed-Soloman error
DQ_BRUSH_MARK = 2               # brush mark > TBD percent
DQ_GRID_SHADOW = 4              # grid shadow mark > TBD percent
DQ_NEAR_EDGE = 8                # spectrum near an edge of the detector
DQ_DEAD = 16                    # dead spot
DQ_HOT = 32                     # hot spot
DQ_BURST = 64                   # count rate implies a burst (FUV only)
DQ_OUT_OF_BOUNDS = 128          # pixel is outside the subarray
DQ_DATA_FILL = 256              # data fill due to telemetry drop-out
DQ_PH_LOW = 512                 # pulse height is below cutoff
DQ_PH_HIGH = 1024               # pulse height is above cutoff
DQ_BAD_TIME = 2048              # time is inside a bad-time interval
DQ_BAD_WAVELENGTH = 4096        # wavelength is below MIN_WAVELENGTH
# DQ_TBD = 8192
# DQ_TBD = 16384
