using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace CSharpAPITest
{
    class Program
    {
        public static void SimpleTest()
        {
            OptoDAQWatcher daqWatcher = new OptoDAQWatcher();
            daqWatcher.Start();
            OptoDAQDescriptor OptoDAQDescriptor;
            do
            {
                OptoDAQDescriptor = daqWatcher.GetFirstDAQ();
            } while (OptoDAQDescriptor.IsValid() == false);

            OptoDAQ optoDAQ = new OptoDAQ(OptoDAQDescriptor);
            if (optoDAQ.Open() == false)
            {
                Console.WriteLine("Could not open DAQ...");
                Console.ReadKey();
                return;
            }
            do
            {
                if (optoDAQ.Is3D())
                {
                    uint sensorCount = optoDAQ.GetSensorCount();
                    OptoPacket3D packet3D = optoDAQ.GetLastPacket3D(false);
                    if (packet3D.IsValid())
                    { 
                        OptoSimplePacket3D simplePacket = packet3D.ToSimplePacket();
                        for (int i = 0; i < (int)sensorCount; ++i)
                        {
                            Console.WriteLine("[SN:{0}] Fx: {1}; Fy {2}; Fz: {3}", i, simplePacket.CountsFx[i], simplePacket.CountsFy[i], simplePacket.CountsFz[i]);
                        }
                    }
                }
                if (optoDAQ.Is6D())
                {
                    optoDAQ.RequestSensitivityReport();
                    OptoPacket6D packet6D = optoDAQ.GetLastPacket6D(false);
                    if (packet6D.IsValid())
                    {
                        OptoSimplePacket6D simplePacket = packet6D.ToSimplePacket();
                        Console.WriteLine("Fx: {0}; Fy {1}; Fz: {2}", simplePacket.Fx, simplePacket.Fy, simplePacket.Fz);
                    }
                }
            } while (optoDAQ.IsValid());
        }

        static void Main(string[] args)
        {
            SimpleTest();
        }
     
    }
}
