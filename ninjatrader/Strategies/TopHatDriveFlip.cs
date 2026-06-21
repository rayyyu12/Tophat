#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

// TopHat NQ — Drive-momentum FLIP backtest for NinjaTrader 8.
//
// Signal (matches research/backtest_flip.py + live engine):
//   Opening range 09:30-09:44 ET (bar open times on a 1-minute chart).
//   or_open  = open of the 09:30 bar
//   or_close = close of the 09:44 bar
//   At 09:45 ET: if or_close > or_open -> LONG, if or_close < or_open -> SHORT, else flat.
//   Entry fill on the NEXT bar open (09:46 on 1-minute data).
//
// Bracket (1 mini):
//   Target 8.5 points (34 ticks)  |  Stop 50 points (200 ticks)
//
// Install: copy to Documents\NinjaTrader 8\bin\Custom\Strategies\
// Chart:   NQ ##-##  |  1-minute  |  RTH session template  |  backtest from ~1 year
//
// Note: Python backtests use 1-second bars; 1-minute NT results will differ slightly
// on intrabar stop/target ordering. Use --legacy-bracket in backtest_flip.py for the
// older 7.5/50 research bracket.

namespace NinjaTrader.NinjaScript.Strategies
{
	public class TopHatDriveFlip : Strategy
	{
		private double orOpen;
		private double orClose;
		private bool orInitialized;
		private bool signalFired;
		private bool enterPending;
		private int pendingDirection; // +1 long, -1 short
		private bool tradedToday;

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description					= "TopHat drive-momentum flip bracket (09:30-09:45 OR, enter 09:45+1 bar).";
				Name						= "TopHatDriveFlip";
				Calculate					= Calculate.OnBarClose;
				EntriesPerDirection			= 1;
				EntryHandling				= EntryHandling.AllEntries;
				IsExitOnSessionCloseStrategy = true;
				ExitOnSessionCloseSeconds	= 30;
				IsFillLimitOnTouch			= false;
				MaximumBarsLookBack			= MaximumBarsLookBack.TwoHundredFiftySix;
				OrderFillResolution			= OrderFillResolution.Standard;
				Slippage					= 0;
				StartBehavior				= StartBehavior.WaitUntilFlat;
				TimeInForce					= TimeInForce.Gtc;
				TraceOrders					= false;
				RealtimeErrorHandling		= RealtimeErrorHandling.StopCancelClose;
				StopTargetHandling			= StopTargetHandling.PerEntryExecution;
				BarsRequiredToTrade			= 20;

				Contracts					= 1;
				TargetPoints				= 8.5;
				StopPoints					= 50.0;
				OrStartTime					= 93000;
				OrEndTime					= 94400;
				SignalTime					= 94500;
				UseLegacyBracket			= false;
			}
			else if (State == State.Configure)
			{
				if (UseLegacyBracket)
				{
					TargetPoints = 7.5;
					StopPoints = 50.0;
				}
			}
		}

		protected override void OnBarUpdate()
		{
			if (CurrentBar < BarsRequiredToTrade)
				return;

			if (Bars.IsFirstBarOfSession)
				ResetDay();

			int barTime = ToTime(Time[0]);

			// Collect opening range (09:30 through 09:44 bar timestamps)
			if (barTime >= OrStartTime && barTime <= OrEndTime)
			{
				if (!orInitialized)
				{
					orOpen = Open[0];
					orInitialized = true;
				}
				orClose = Close[0];
			}

			// Lock drive direction on first bar at/after 09:45
			if (!signalFired && barTime >= SignalTime)
			{
				signalFired = true;
				if (orInitialized)
				{
					if (orClose > orOpen)
						pendingDirection = 1;
					else if (orClose < orOpen)
						pendingDirection = -1;
					else
						pendingDirection = 0;

					if (pendingDirection != 0 && !tradedToday)
						enterPending = true;
				}
			}

			// Next-bar-open entry (signal bar -> following bar)
			if (enterPending && Position.MarketPosition == MarketPosition.Flat)
			{
				enterPending = false;
				int targetTicks = PointsToTicks(TargetPoints);
				int stopTicks = PointsToTicks(StopPoints);

				if (pendingDirection > 0)
				{
					SetProfitTarget("TopHatFlipLong", CalculationMode.Ticks, targetTicks);
					SetStopLoss("TopHatFlipLong", CalculationMode.Ticks, stopTicks, false);
					EnterLong(Contracts, "TopHatFlipLong");
				}
				else if (pendingDirection < 0)
				{
					SetProfitTarget("TopHatFlipShort", CalculationMode.Ticks, targetTicks);
					SetStopLoss("TopHatFlipShort", CalculationMode.Ticks, stopTicks, false);
					EnterShort(Contracts, "TopHatFlipShort");
				}
				tradedToday = true;
			}
		}

		private void ResetDay()
		{
			orOpen = 0;
			orClose = 0;
			orInitialized = false;
			signalFired = false;
			enterPending = false;
			pendingDirection = 0;
			tradedToday = false;
		}

		private int PointsToTicks(double points)
		{
			if (Instrument.MasterInstrument.TickSize <= 0)
				return Math.Max(1, (int)Math.Round(points / 0.25));
			return Math.Max(1, (int)Math.Round(points / Instrument.MasterInstrument.TickSize));
		}

		#region Properties
		[NinjaScriptProperty]
		[Range(1, int.MaxValue)]
		[Display(Name = "Contracts", Order = 1, GroupName = "TopHat")]
		public int Contracts { get; set; }

		[NinjaScriptProperty]
		[Range(0.25, 500)]
		[Display(Name = "TargetPoints", Order = 2, GroupName = "TopHat")]
		public double TargetPoints { get; set; }

		[NinjaScriptProperty]
		[Range(0.25, 500)]
		[Display(Name = "StopPoints", Order = 3, GroupName = "TopHat")]
		public double StopPoints { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "OrStartTime", Description = "OR start HHmmss ET (93000 = 09:30)", Order = 4, GroupName = "TopHat")]
		public int OrStartTime { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "OrEndTime", Description = "Last OR bar time HHmmss (94400 = 09:44)", Order = 5, GroupName = "TopHat")]
		public int OrEndTime { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "SignalTime", Description = "Drive lock time HHmmss (94500 = 09:45)", Order = 6, GroupName = "TopHat")]
		public int SignalTime { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "UseLegacyBracket", Description = "7.5 pt target / 50 pt stop (old research bracket)", Order = 7, GroupName = "TopHat")]
		public bool UseLegacyBracket { get; set; }
		#endregion
	}
}
