#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

// TopHat NQ — Drive-momentum NUKE backtest for NinjaTrader 8.
//
// Same drive signal as TopHatDriveFlip (09:30-09:45 ET opening-range direction,
// entry on the bar after 09:45).
//
// Bracket (2 minis, $4k target, $1k DLL — live engine / nukeDLL backtest):
//   Target 100 points (400 ticks)  |  Stop 25 points (100 ticks)
//
// Install: copy to Documents\NinjaTrader 8\bin\Custom\Strategies\
// Chart:   NQ ##-##  |  1-minute  |  RTH session template

namespace NinjaTrader.NinjaScript.Strategies
{
	public class TopHatDriveNuke : Strategy
	{
		private double orOpen;
		private double orClose;
		private bool orInitialized;
		private bool signalFired;
		private bool enterPending;
		private int pendingDirection;
		private bool tradedToday;

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description					= "TopHat drive-momentum nuke bracket (09:30-09:45 OR, enter 09:45+1 bar).";
				Name						= "TopHatDriveNuke";
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

				Contracts					= 2;
				TargetPoints				= 100.0;
				StopPoints					= 25.0;
				OrStartTime					= 93000;
				OrEndTime					= 94400;
				SignalTime					= 94500;
				UseNoDllBracket				= false;
			}
			else if (State == State.Configure)
			{
				if (UseNoDllBracket)
					StopPoints = 50.0;
			}
		}

		protected override void OnBarUpdate()
		{
			if (CurrentBar < BarsRequiredToTrade)
				return;

			if (Bars.IsFirstBarOfSession)
				ResetDay();

			int barTime = ToTime(Time[0]);

			if (barTime >= OrStartTime && barTime <= OrEndTime)
			{
				if (!orInitialized)
				{
					orOpen = Open[0];
					orInitialized = true;
				}
				orClose = Close[0];
			}

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

			if (enterPending && Position.MarketPosition == MarketPosition.Flat)
			{
				enterPending = false;
				int targetTicks = PointsToTicks(TargetPoints);
				int stopTicks = PointsToTicks(StopPoints);

				if (pendingDirection > 0)
				{
					SetProfitTarget("TopHatNukeLong", CalculationMode.Ticks, targetTicks);
					SetStopLoss("TopHatNukeLong", CalculationMode.Ticks, stopTicks, false);
					EnterLong(Contracts, "TopHatNukeLong");
				}
				else if (pendingDirection < 0)
				{
					SetProfitTarget("TopHatNukeShort", CalculationMode.Ticks, targetTicks);
					SetStopLoss("TopHatNukeShort", CalculationMode.Ticks, stopTicks, false);
					EnterShort(Contracts, "TopHatNukeShort");
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
		[Display(Name = "OrStartTime", Order = 4, GroupName = "TopHat")]
		public int OrStartTime { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "OrEndTime", Order = 5, GroupName = "TopHat")]
		public int OrEndTime { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "SignalTime", Order = 6, GroupName = "TopHat")]
		public int SignalTime { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "UseNoDllBracket", Description = "100 pt target / 50 pt stop (2:1 no-DLL research bracket)", Order = 7, GroupName = "TopHat")]
		public bool UseNoDllBracket { get; set; }
		#endregion
	}
}
